from typing import Any, Callable, Dict, Optional, Tuple, Union

import torch
from torch import Tensor

from sbi.inference.posteriors.base_posterior import NeuralPosterior
from sbi.inference.ibsnre.snre_base import RatioEstimator
from sbi.types import TensorboardSummaryWriter
from sbi.utils import del_entries


class SNRE_IB_A(RatioEstimator):
    def __init__(
        self,
        prior: Optional[Any] = None,
        classifier: Union[str, Callable] = "resnet",
        device: str = "cpu",
        logging_level: Union[int, str] = "warning",
        summary_writer: Optional[TensorboardSummaryWriter] = None,
        show_progress_bars: bool = True,
        **unused_args
    ):
        r"""AALR[1], here known as SNRE_A.

        [1] _Likelihood-free MCMC with Amortized Approximate Likelihood Ratios_, Hermans
            et al., ICML 2020, https://arxiv.org/abs/1903.04057

        Args:
            prior: A probability distribution that expresses prior knowledge about the
                parameters, e.g. which ranges are meaningful for them. If `None`, the
                prior must be passed to `.build_posterior()`.
            classifier: Classifier trained to approximate likelihood ratios. If it is
                a string, use a pre-configured network of the provided type (one of
                linear, mlp, resnet). Alternatively, a function that builds a custom
                neural network can be provided. The function will be called with the
                first batch of simulations (theta, x), which can thus be used for shape
                inference and potentially for z-scoring. It needs to return a PyTorch
                `nn.Module` implementing the classifier.
            device: Training device, e.g., "cpu", "cuda" or "cuda:{0, 1, ...}".
            logging_level: Minimum severity of messages to log. One of the strings
                INFO, WARNING, DEBUG, ERROR and CRITICAL.
            summary_writer: A tensorboard `SummaryWriter` to control, among others, log
                file location (default is `<current working directory>/logs`.)
            show_progress_bars: Whether to show a progressbar during simulation and
                sampling.
            unused_args: Absorbs additional arguments. No entries will be used. If it
                is not empty, we warn. In future versions, when the new interface of
                0.14.0 is more mature, we will remove this argument.
        """

        kwargs = del_entries(locals(), entries=("self", "__class__", "unused_args"))
        super().__init__(**kwargs, **unused_args)

    def train(
        self,
        training_batch_size: int = 50,
        learning_rate: float = 5e-4,
        lagrange_multiplier: Optional[float] = None,
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
    ) -> NeuralPosterior:
        r"""Return classifier that approximates the ratio $p(\theta,x)/p(\theta)p(x)$.

        Args:
            training_batch_size: Training batch size.
            learning_rate: Learning rate for Adam optimizer.
            lagrange_multiplier:
            validation_fraction: The fraction of data to use for validation.
            stop_after_epochs: The number of epochs to wait for improvement on the
                validation set before terminating training.
            max_num_epochs: Maximum number of epochs to run. If reached, we stop
                training even when the validation loss is still decreasing. If None, we
                train until validation loss increases (see also `stop_after_epochs`).
            clip_max_norm: Value at which to clip the total gradient norm in order to
                prevent exploding gradients. Use None for no clipping.
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
            show_train_summary: Whether to print the number of epochs and validation
                loss and leakage after the training.
            dataloader_kwargs: Additional or updated kwargs to be passed to the training
                and validation dataloaders (like, e.g., a collate_fn)

        Returns:
            Classifier that approximates the ratio $p(\theta,x)/p(\theta)p(x)$.
        """

        # AALR is defined for `num_atoms=2`.
        # Proxy to `super().__call__` to ensure right parameter.
        kwargs = del_entries(locals(), entries=("self", "__class__"))
        return super().train(**kwargs, num_atoms=2)

    def anneal(
        self,
        training_batch_size: int = 50,
        learning_rate: float = 5e-4,
        lagrange_multiplier: Optional[float] = None,
        annealing_rate: float = 0.1,
        validation_fraction: float = 0.1,
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
            training_batch_size: Training batch size.
            learning_rate: Learning rate for Adam optimizer.
            lagrange_multiplier:
            annealing_rate:
            validation_fraction: The fraction of data to use for validation.
            max_num_epochs: Maximum number of epochs to run. If reached, we stop
                training even when the validation loss is still decreasing.
            clip_max_norm: Value at which to clip the total gradient norm in order to
                prevent exploding gradients. Use None for no clipping.
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
            show_train_summary: Whether to print the number of epochs and validation
                loss and leakage after the training.
            dataloader_kwargs: Additional or updated kwargs to be passed to the training
                and validation dataloaders (like, e.g., a collate_fn)

        Returns:
            Classifier that approximates the ratio $p(\theta,x)/p(\theta)p(x)$.
        """

        # AALR is defined for `num_atoms=2`.
        # Proxy to `super().__call__` to ensure right parameter.
        kwargs = del_entries(locals(), entries=("self", "__class__"))
        return super().anneal(**kwargs, num_atoms=2)

    @staticmethod
    def _correction(logits: Tensor, dim: int) -> Tensor:
        """compute the correction term"""
        m = torch.tensor(logits.size(dim), dtype=logits.dtype, device=logits.device)
        neg_h = -torch.binary_cross_entropy_with_logits(logits, torch.sigmoid(logits))
        return torch.logsumexp(neg_h, dim=dim) - torch.log(m)

    def _loss_terms(
        self, theta: Tensor, x: Tensor, num_atoms: int
    ) -> Tuple[Tensor, Tensor, Tensor]:
        """Returns the binary cross-entropy loss for the trained classifier.
        The classifier takes as input a $(\theta,x)$ pair. It is trained to predict 1
        if the pair was sampled from the joint $p(\theta,x)$, and to predict 0 if the
        pair was sampled from the marginals $p(\theta)p(x)$.
        """

        assert theta.shape[0] == x.shape[0], "Batch sizes for theta and x must match."
        batch_size = theta.shape[0]

        logits = self._classifier_logits(theta, x, num_atoms).squeeze()

        # entropy yw
        # Alternating pairs where there is one sampled from the joint and one
        # sampled from the marginals. The first element is sampled from the
        # joint p(theta, x) and is labelled 1. The second element is sampled
        # from the marginals p(theta)p(x) and is labelled 0. And so on.
        labels = torch.ones(2 * batch_size, device=theta.device)  # two atoms
        labels[1::2] = 0.0
        entropy_yw = torch.binary_cross_entropy_with_logits(logits, labels)

        # entropy zw
        entropy_zw = torch.binary_cross_entropy_with_logits(
            logits, torch.sigmoid(logits)
        )

        # correction term
        logits_ = self._correction_logits(theta, x, num_atoms)
        exp_neg_entropy_zw = self._correction(logits_, dim=-1)

        return (
            entropy_yw,
            entropy_zw,
            exp_neg_entropy_zw,
        )
