from abc import ABC, abstractmethod
from copy import deepcopy
from math import exp
from typing import Any, Callable, Dict, Optional, Tuple, Union

import torch
from torch import Tensor, eye, nn, ones, optim
from torch.nn.utils import clip_grad_norm_
from torch.utils import data
from torch.utils.tensorboard import SummaryWriter

from sbi import utils as utils
from sbi.inference.base import NeuralInference
from sbi.inference.posteriors import MCMCPosterior, RejectionPosterior
from sbi.inference.potentials import ratio_estimator_based_potential
from sbi.types import TorchModule
from sbi.utils import (
    check_estimator_arg,
    clamp_and_warn,
    validate_theta_and_x,
    x_shape_from_simulation,
)
from sbi.utils.sbiutils import mask_sims_from_prior
from sbi.utils.user_input_checks import process_x


class RatioEstimator(NeuralInference, ABC):
    def __init__(
        self,
        prior: Optional[Any] = None,
        classifier: Union[str, Callable] = "resnet",
        device: str = "cpu",
        logging_level: Union[int, str] = "warning",
        summary_writer: Optional[SummaryWriter] = None,
        show_progress_bars: bool = True,
        **unused_args
    ):
        r"""Sequential Neural Ratio Estimation.

        We implement two inference methods in the respective subclasses.

        - SNRE_A / AALR is limited to `num_atoms=2`, but allows for density evaluation
          when training for one round.
        - SNRE_B / SRE can use more than two atoms, potentially boosting performance,
          but allows for posterior evaluation **only up to a normalizing constant**,
          even when training only one round.

        Args:
            classifier: Classifier trained to approximate likelihood ratios. If it is
                a string, use a pre-configured network of the provided type (one of
                linear, mlp, resnet). Alternatively, a function that builds a custom
                neural network can be provided. The function will be called with the
                first batch of simulations (theta, x), which can thus be used for shape
                inference and potentially for z-scoring. It needs to return a PyTorch
                `nn.Module` implementing the classifier.
            unused_args: Absorbs additional arguments. No entries will be used. If it
                is not empty, we warn. In future versions, when the new interface of
                0.14.0 is more mature, we will remove this argument.

        See docstring of `NeuralInference` class for all other arguments.
        """

        super().__init__(
            prior=prior,
            device=device,
            logging_level=logging_level,
            summary_writer=summary_writer,
            show_progress_bars=show_progress_bars,
            **unused_args
        )
        self._summary = dict(
            median_observation_distances=[],
            epochs=[],
            best_validation_log_probs=[],
            validation_log_probs=[],
            validation_objective_log_probs=[],
            validation_regularizer_log_probs=[],
            validation_ratio_avg=[],
            train_log_probs=[],
            epoch_durations_sec=[],
        )

        # As detailed in the docstring, `density_estimator` is either a string or
        # a callable. The function creating the neural network is attached to
        # `_build_neural_net`. It will be called in the first round and receive
        # thetas and xs as inputs, so that they can be used for shape inference and
        # potentially for z-scoring.
        check_estimator_arg(classifier)
        if isinstance(classifier, str):
            self._build_neural_net = utils.classifier_nn(model=classifier)
        else:
            self._build_neural_net = classifier

        # Ratio-based-specific summary_writer fields.
        self._summary.update({"mcmc_times": []})  # type: ignore

    def append_simulations(
        self,
        theta: Tensor,
        x: Tensor,
        from_round: int = 0,
    ) -> "RatioEstimator":
        r"""Store parameters and simulation outputs to use them for later training.

        Data are stored as entries in lists for each type of variable (parameter/data).

        Stores $\theta$, $x$, prior_masks (indicating if simulations are coming from the
        prior or not) and an index indicating which round the batch of simulations came
        from.

        Args:
            theta: Parameter sets.
            x: Simulation outputs.
            from_round: Which round the data stemmed from. Round 0 means from the prior.
                With default settings, this is not used at all for `SNRE`. Only when
                the user later on requests `.train(discard_prior_samples=True)`, we
                use these indices to find which training data stemmed from the prior.
        Returns:
            NeuralInference object (returned so that this function is chainable).
        """

        theta, x = validate_theta_and_x(theta, x, training_device=self._device)

        self._theta_roundwise.append(theta)
        self._x_roundwise.append(x)
        self._prior_masks.append(mask_sims_from_prior(int(from_round), theta.size(0)))
        self._data_round_index.append(int(from_round))

        return self

    def train(
        self,
        num_atoms: int = 10,
        training_batch_size: int = 50,
        learning_rate: float = 5e-4,
        lagrange_multiplier: float = 0.0,
        validation_fraction: float = 0.1,
        stop_after_epochs: int = 20,
        max_num_epochs: Optional[int] = None,
        clip_max_norm: Optional[float] = 5.0,
        exclude_invalid_x: bool = True,
        resume_training: bool = False,
        discard_prior_samples: bool = False,
        retrain_from_scratch: bool = False,
        show_train_summary: bool = False,
        dataloader_kwargs: Optional[Dict] = None,
    ) -> nn.Module:
        r"""Return classifier that approximates the ratio $p(\theta,x)/p(\theta)p(x)$.

        Args:
            num_atoms: Number of atoms to use for classification.
            exclude_invalid_x: Whether to exclude simulation outputs `x=NaN` or `x=±∞`
                during training. Expect errors, silent or explicit, when `False`.
            resume_training: Can be used in case training time is limited, e.g. on a
                cluster. If `True`, the split between train and validation set, the
                optimizer, the number of epochs, and the best validation log-prob will
                be restored from the last time `.train()` was called.
            discard_prior_samples: Whether to discard samples simulated in round 1, i.e.
                from the prior. Training may be sped up by ignoring such less targeted
                samples.
            retrain_from_scratch: Whether to retrain the conditional density
                estimator for the posterior from scratch each round.
            dataloader_kwargs: Additional or updated kwargs to be passed to the training
                and validation dataloaders (like, e.g., a collate_fn)

        Returns:
            Classifier that approximates the ratio $p(\theta,x)/p(\theta)p(x)$.
        """

        max_num_epochs = 2 ** 31 - 1 if max_num_epochs is None else max_num_epochs

        # Starting index for the training set (1 = discard round-0 samples).
        start_idx = int(discard_prior_samples and self._round > 0)
        # Load data from most recent round.
        self._round = max(self._data_round_index)
        theta, x, _ = self.get_simulations(
            start_idx, exclude_invalid_x, warn_on_invalid=True
        )

        # Dataset is shared for training and validation loaders.
        dataset = data.TensorDataset(theta, x)

        train_loader, val_loader = self.get_dataloaders(
            dataset,
            training_batch_size,
            validation_fraction,
            resume_training,
            dataloader_kwargs=dataloader_kwargs,
        )

        clipped_batch_size = min(training_batch_size, len(val_loader))

        num_atoms = clamp_and_warn(
            "num_atoms", num_atoms, min_val=2, max_val=clipped_batch_size
        )

        # First round or if retraining from scratch:
        # Call the `self._build_neural_net` with the rounds' thetas and xs as
        # arguments, which will build the neural network
        # This is passed into NeuralPosterior, to create a neural posterior which
        # can `sample()` and `log_prob()`. The network is accessible via `.net`.
        if self._neural_net is None or retrain_from_scratch:
            self._neural_net = self._build_neural_net(
                theta[self.train_indices], x[self.train_indices]
            )
            self._x_shape = x_shape_from_simulation(x)

        self._neural_net.to(self._device)

        if not resume_training:
            self.optimizer = optim.Adam(
                list(self._neural_net.parameters()),
                lr=learning_rate,
            )
            self.epoch, self._val_log_prob = 0, float("-Inf")

        while self.epoch < max_num_epochs and not self._converged(
            self.epoch, stop_after_epochs
        ):

            # Train for a single epoch.
            self._neural_net.train()
            train_log_probs_sum = 0
            for batch in train_loader:
                self.optimizer.zero_grad()
                theta_batch, x_batch = (
                    batch[0].to(self._device),
                    batch[1].to(self._device),
                )

                _, _, train_losses = self._loss(
                    theta_batch, x_batch, num_atoms, lagrange_multiplier
                )
                train_loss = torch.mean(train_losses)
                train_log_probs_sum -= train_losses.sum().item()

                train_loss.backward()
                if clip_max_norm is not None:
                    clip_grad_norm_(
                        self._neural_net.parameters(),
                        max_norm=clip_max_norm,
                    )
                self.optimizer.step()

            self.epoch += 1

            train_log_prob_average = train_log_probs_sum / (
                len(train_loader) * train_loader.batch_size
            )
            self._summary["train_log_probs"].append(train_log_prob_average)

            # Calculate validation performance.
            self._neural_net.eval()
            val_log_prob_sum = 0
            val_obj_sum = 0
            val_reg_sum = 0
            val_ratio_sum = 0
            with torch.no_grad():
                for batch in val_loader:
                    theta_batch, x_batch = (
                        batch[0].to(self._device),
                        batch[1].to(self._device),
                    )
                    val_objective, val_regularizer, val_losses = self._loss(
                        theta_batch, x_batch, num_atoms, lagrange_multiplier
                    )
                    val_ratio = self._neural_net([theta_batch, x_batch])

                    val_ratio_sum += val_ratio.sum().item()
                    val_obj_sum -= val_objective.sum().item()
                    val_reg_sum -= val_regularizer.sum().item()
                    val_log_prob_sum -= val_losses.sum().item()
                # Take mean over all validation samples.
                n = len(val_loader) * val_loader.batch_size
                val_ratio_avg = val_ratio_sum / n
                val_obj_avg = val_obj_sum / n
                val_reg_avg = val_reg_sum / n
                self._val_log_prob = val_log_prob_sum / n
                # Log validation log prob for every epoch.
                self._summary["validation_ratio_avg"].append(val_ratio_avg)
                self._summary["validation_objective_log_probs"].append(val_obj_avg)
                self._summary["validation_regularizer_log_probs"].append(val_reg_avg)
                self._summary["validation_log_probs"].append(self._val_log_prob)

            self._maybe_show_progress(self._show_progress_bars, self.epoch)

        self._report_convergence_at_end(self.epoch, stop_after_epochs, max_num_epochs)

        # Update summary.
        self._summary["epochs"].append(self.epoch)
        self._summary["best_validation_log_probs"].append(self._best_val_log_prob)

        # Update TensorBoard and summary dict.
        self._summarize(
            round_=self._round,
            x_o=None,
            theta_bank=theta,
            x_bank=x,
        )

        # Update description for progress bar.
        if show_train_summary:
            print(self._describe_round(self._round, self._summary))

        return deepcopy(self._neural_net)

    def anneal(
        self,
        num_atoms: int = 10,
        training_batch_size: int = 50,
        learning_rate: float = 5e-4,
        lagrange_multiplier: Optional[float] = None,
        annealing_rate: float = 0.1,
        validation_fraction: float = 0.1,
        stop_after_epochs: int = 2 ** 31 - 1,
        max_num_epochs: int = 100,
        clip_max_norm: Optional[float] = 5.0,
        exclude_invalid_x: bool = True,
        resume_training: bool = False,
        discard_prior_samples: bool = False,
        retrain_from_scratch: bool = False,
        show_train_summary: bool = False,
        dataloader_kwargs: Optional[Dict] = None,
    ) -> nn.Module:
        r"""Return classifier that approximates the ratio $p(\theta,x)/p(\theta)p(x)$.

        Args:
            num_atoms: Number of atoms to use for classification.
            exclude_invalid_x: Whether to exclude simulation outputs `x=NaN` or `x=±∞`
                during training. Expect errors, silent or explicit, when `False`.
            resume_training: Can be used in case training time is limited, e.g. on a
                cluster. If `True`, the split between train and validation set, the
                optimizer, the number of epochs, and the best validation log-prob will
                be restored from the last time `.train()` was called.
            discard_prior_samples: Whether to discard samples simulated in round 1, i.e.
                from the prior. Training may be sped up by ignoring such less targeted
                samples.
            retrain_from_scratch: Whether to retrain the conditional density
                estimator for the posterior from scratch each round.
            dataloader_kwargs: Additional or updated kwargs to be passed to the training
                and validation dataloaders (like, e.g., a collate_fn)

        Returns:
            Classifier that approximates the ratio $p(\theta,x)/p(\theta)p(x)$.
        """
        assert annealing_rate > 0.0
        epsilon = 0.01
        law_of_cooling = lambda x: lagrange_multiplier + (
            epsilon - lagrange_multiplier
        ) * exp(-annealing_rate * x)

        # Starting index for the training set (1 = discard round-0 samples).
        start_idx = int(discard_prior_samples and self._round > 0)
        # Load data from most recent round.
        self._round = max(self._data_round_index)
        theta, x, _ = self.get_simulations(
            start_idx, exclude_invalid_x, warn_on_invalid=True
        )

        # Dataset is shared for training and validation loaders.
        dataset = data.TensorDataset(theta, x)

        train_loader, val_loader = self.get_dataloaders(
            dataset,
            training_batch_size,
            validation_fraction,
            resume_training,
            dataloader_kwargs=dataloader_kwargs,
        )

        clipped_batch_size = min(training_batch_size, len(val_loader))

        num_atoms = clamp_and_warn(
            "num_atoms", num_atoms, min_val=2, max_val=clipped_batch_size
        )

        # First round or if retraining from scratch:
        # Call the `self._build_neural_net` with the rounds' thetas and xs as
        # arguments, which will build the neural network
        # This is passed into NeuralPosterior, to create a neural posterior which
        # can `sample()` and `log_prob()`. The network is accessible via `.net`.
        if self._neural_net is None or retrain_from_scratch:
            self._neural_net = self._build_neural_net(
                theta[self.train_indices], x[self.train_indices]
            )
            self._x_shape = x_shape_from_simulation(x)

        self._neural_net.to(self._device)

        if not resume_training:
            self.optimizer = optim.Adam(
                list(self._neural_net.parameters()),
                lr=learning_rate,
            )
            self.epoch, self._val_log_prob = 0, float("-Inf")

        while self.epoch < max_num_epochs:
            # TODO this is a hack
            # split the function so each internal part is called seperately
            # then I can set stop_after_epochs to None when unused.
            self._converged(self.epoch, stop_after_epochs)

            annealed_lagrange_multiplier = law_of_cooling(self.epoch)

            # Train for a single epoch.
            self._neural_net.train()
            train_log_probs_sum = 0
            for batch in train_loader:
                self.optimizer.zero_grad()
                theta_batch, x_batch = (
                    batch[0].to(self._device),
                    batch[1].to(self._device),
                )

                train_losses = self._loss(
                    theta_batch, x_batch, num_atoms, annealed_lagrange_multiplier
                )
                train_loss = torch.mean(train_losses)
                train_log_probs_sum -= train_losses.sum().item()

                train_loss.backward()
                if clip_max_norm is not None:
                    clip_grad_norm_(
                        self._neural_net.parameters(),
                        max_norm=clip_max_norm,
                    )
                self.optimizer.step()

            self.epoch += 1

            train_log_prob_average = train_log_probs_sum / (
                len(train_loader) * train_loader.batch_size
            )
            self._summary["train_log_probs"].append(train_log_prob_average)

            # Calculate validation performance.
            self._neural_net.eval()
            val_log_prob_sum = 0
            with torch.no_grad():
                for batch in val_loader:
                    theta_batch, x_batch = (
                        batch[0].to(self._device),
                        batch[1].to(self._device),
                    )
                    val_losses = self._loss(
                        theta_batch, x_batch, num_atoms, lagrange_multiplier
                    )
                    val_log_prob_sum -= val_losses.sum().item()
                # Take mean over all validation samples.
                self._val_log_prob = val_log_prob_sum / (
                    len(val_loader) * val_loader.batch_size
                )
                # Log validation log prob for every epoch.
                self._summary["validation_log_probs"].append(self._val_log_prob)

            self._maybe_show_progress(self._show_progress_bars, self.epoch)

        self._report_convergence_at_end(self.epoch, stop_after_epochs, max_num_epochs)

        # Update summary.
        self._summary["epochs"].append(self.epoch)
        self._summary["best_validation_log_probs"].append(self._best_val_log_prob)

        # Update TensorBoard and summary dict.
        self._summarize(
            round_=self._round,
            x_o=None,
            theta_bank=theta,
            x_bank=x,
        )

        # Update description for progress bar.
        if show_train_summary:
            print(self._describe_round(self._round, self._summary))

        return deepcopy(self._neural_net)

    @staticmethod
    def _expand_exclude_same(tensor: Tensor) -> Tensor:
        """expand a tensor to a leading matrix then remove the diagonal element."""
        batch_size = tensor.shape[0]
        reduced_batch_size = batch_size - 1

        # produce a [b, b, ...] shaped vector of data
        tt = tensor.expand(batch_size, batch_size, *tensor.shape[1:])

        # remove the "diagonal" elements from the [b, b] section of the vector
        # reshape the output into [b, b-1, ...] with the "diagonal element" removed
        return tt[~torch.eye(batch_size, dtype=torch.bool), ...].reshape(
            batch_size, reduced_batch_size, *tensor.shape[1:]
        )

    def _classifier_logits(self, theta: Tensor, x: Tensor, num_atoms: int) -> Tensor:
        """Return logits obtained through classifier forward pass.

        The logits are obtained from atomic sets of (theta,x) pairs.
        """
        batch_size = theta.shape[0]
        repeated_x = utils.repeat_rows(x, num_atoms)

        # Choose `1` or `num_atoms - 1` thetas from the rest of the batch for each x.
        probs = ones(batch_size, batch_size) * (1 - eye(batch_size)) / (batch_size - 1)

        choices = torch.multinomial(probs, num_samples=num_atoms - 1, replacement=False)

        contrasting_theta = theta[choices]

        atomic_theta = torch.cat((theta[:, None, :], contrasting_theta), dim=1).reshape(
            batch_size * num_atoms, -1
        )

        return self._neural_net([atomic_theta, repeated_x])

    def _correction_logits(self, theta: Tensor, x: Tensor, num_atoms: int) -> Tensor:
        """Return logits obtained through a classifier forward pass on the correction term.

        the logits are obtained from repeated atomic sets of (theta,x) pairs
        """
        batch_size = theta.shape[0]
        reduced_batch_size = batch_size - 1

        # The purpose of this section is to compute the expected value in the correction term
        # we want to avoid using the same piece of data for computing the expectation value
        # since that would bias the estimate.
        xx = self._expand_exclude_same(x).reshape(
            batch_size * reduced_batch_size, *x.shape[1:]
        )
        tt = self._expand_exclude_same(theta)

        repeated_xx = utils.repeat_rows(xx, num_atoms)

        # Choose `1` or `num_atoms - 1` thetas from the rest of the batch for each x.
        probs = (
            torch.ones(reduced_batch_size, reduced_batch_size)
            * (1 - torch.eye(reduced_batch_size))
            / (reduced_batch_size - 1)
        )

        choices = torch.stack(
            [
                torch.multinomial(probs, num_samples=num_atoms - 1, replacement=False)
                for _ in range(batch_size)
            ]
        )

        contrasting_theta = theta[choices]

        atomic_theta = torch.cat((tt[:, :, None, :], contrasting_theta), dim=2).reshape(
            batch_size * reduced_batch_size * num_atoms, -1
        )

        return self._neural_net([atomic_theta, repeated_xx]).reshape(
            num_atoms * batch_size, reduced_batch_size
        )

    @staticmethod
    @abstractmethod
    def _correction(logits: Tensor, dim: int) -> Tensor:
        raise NotImplementedError

    @staticmethod
    def convex_combine(
        entropy_yw: Tensor,
        entropy_zw: Tensor,
        exp_neg_entropy_zw: Tensor,
        lagrange_multiplier: float,
    ) -> Tensor:
        return (1 - lagrange_multiplier) * entropy_yw - lagrange_multiplier * (
            entropy_zw + exp_neg_entropy_zw
        )

    @abstractmethod
    def _loss_terms(
        theta: Tensor, x: Tensor, num_atoms: int
    ) -> Tuple[Tensor, Tensor, Tensor]:
        raise NotImplementedError

    def _loss(
        self,
        theta: Tensor,
        x: Tensor,
        num_atoms: int,
        lagrange_multiplier: float,
    ) -> Tuple[Tensor, Tensor, Tensor]:
        """objective, regularizer, mixed"""
        entropy_yw, entropy_zw, exp_neg_entropy_zw = self._loss_terms(
            theta, x, num_atoms
        )
        return (
            entropy_yw,
            entropy_zw + exp_neg_entropy_zw,
            self.convex_combine(
                entropy_yw, entropy_zw, exp_neg_entropy_zw, lagrange_multiplier
            ),
        )

    def build_posterior(
        self,
        density_estimator: Optional[TorchModule] = None,
        prior: Optional[Any] = None,
        sample_with: str = "mcmc",
        mcmc_method: str = "slice_np",
        mcmc_parameters: Dict[str, Any] = {},
        rejection_sampling_parameters: Dict[str, Any] = {},
    ) -> Union[MCMCPosterior, RejectionPosterior]:
        r"""Build posterior from the neural density estimator.

        SNRE trains a neural network to approximate likelihood ratios. The
        posterior wraps the trained network such that one can directly evaluate the
        unnormalized posterior log probability $p(\theta|x) \propto p(x|\theta) \cdot
        p(\theta)$ and draw samples from the posterior with MCMC or rejection sampling.
        Note that, in the case of single-round SNRE_A / AALR, it is possible to
        evaluate the log-probability of the **normalized** posterior, but sampling
        still requires MCMC (or rejection sampling).

        Args:
            density_estimator: The density estimator that the posterior is based on.
                If `None`, use the latest neural density estimator that was trained.
            prior: Prior distribution.
            sample_with: Method to use for sampling from the posterior. Must be one of
                [`mcmc` | `rejection`].
            mcmc_method: Method used for MCMC sampling, one of `slice_np`, `slice`,
                `hmc`, `nuts`. Currently defaults to `slice_np` for a custom numpy
                implementation of slice sampling; select `hmc`, `nuts` or `slice` for
                Pyro-based sampling.
            mcmc_parameters: Additional kwargs passed to `MCMCPosterior`.
            rejection_sampling_parameters: Additional kwargs passed to
                `RejectionPosterior`.
        Returns:
            Posterior $p(\theta|x)$  with `.sample()` and `.log_prob()` methods
            (the returned log-probability is unnormalized).
        """
        if prior is None:
            assert (
                self._prior is not None
            ), "You did not pass a prior. You have to pass the prior either at initialization `inference = SNRE(prior)` or to `.build_posterior(prior=prior)`."
            prior = self._prior

        if density_estimator is None:
            density_estimator = self._neural_net
            # If internal net is used device is defined.
            device = self._device
        else:
            # Otherwise, infer it from the device of the net parameters.
            device = next(density_estimator.parameters()).device.type

        potential_fn, theta_transform = ratio_estimator_based_potential(
            ratio_estimator=self._neural_net, prior=prior, x_o=None
        )

        if sample_with == "mcmc":
            self._posterior = MCMCPosterior(
                potential_fn=potential_fn,
                theta_transform=theta_transform,
                proposal=prior,
                method=mcmc_method,
                device=device,
                x_shape=self._x_shape,
                **mcmc_parameters
            )
        elif sample_with == "rejection":
            self._posterior = RejectionPosterior(
                potential_fn=potential_fn,
                proposal=prior,
                device=device,
                x_shape=self._x_shape,
                **rejection_sampling_parameters
            )
        elif sample_with == "vi":
            raise NotImplementedError
        else:
            raise NotImplementedError

        # Store models at end of each round.
        self._model_bank.append(deepcopy(self._posterior))

        return deepcopy(self._posterior)
