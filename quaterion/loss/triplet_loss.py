from typing import Optional

import torch
import torch.nn.functional as F
from torch import LongTensor, Tensor

from quaterion.distances import Distance
from quaterion.loss.group_loss import GroupLoss
from quaterion.utils import (
    get_anchor_negative_mask,
    get_anchor_positive_mask,
    get_triplet_mask,
)


class TripletLoss(GroupLoss):
    """Implements Triplet Loss as defined in https://arxiv.org/abs/1503.03832

    It supports batch-all and batch-hard strategies for online triplet mining.

    Args:
        margin: Margin value to push negative examples
            apart. Optional, defaults to `0.5`.
        distance_metric_name: Name of the distance function, e.g.,
            :class:`~quaterion.distances.Distance`. Optional, defaults to
            :attr:`~quaterion.distances.Distance.COSINE`.
        mining (str, optional): Triplet mining strategy. One of
            `"all"`, `"hard"`. Defaults to `"hard"`.
    """

    def __init__(
        self,
        margin: Optional[float] = 1.0,
        distance_metric_name: Distance = Distance.COSINE,
        mining: Optional[str] = "hard",
    ):
        mining_types = ["all", "hard"]
        if mining not in mining_types:
            raise ValueError(
                f"Unrecognized mining strategy: {mining}. Must be one of {', '.join(mining_types)}"
            )
        super(TripletLoss, self).__init__(distance_metric_name=distance_metric_name)

        self._margin = margin
        self._mining = mining

    def get_config_dict(self):
        config = super().get_config_dict()
        config.update(
            {
                "margin": self._margin,
                "mining": self._mining,
            }
        )

        return config

    def forward(
        self,
        embeddings: Tensor,
        groups: LongTensor,
        memory_embeddings: Optional[Tensor] = None,
        memory_groups: Optional[LongTensor] = None,
    ) -> Tensor:
        """Calculates Triplet Loss with specified embeddings and labels.

        Args:
            embeddings: shape: (batch_size, vector_length) - Batch of embeddings.
            groups: shape: (batch_size,) - Batch of labels associated with `embeddings`
            memory_embeddings: shape: (memory_buffer_size, vector_length) - Embeddings stored
                in a ring buffer. Used only for XBM
            memory_groups: shape: (memory_buffer_size,) - Groups ids associated with `memory_embeddings`.
                Used only for XBM

        Returns:
            torch.Tensor: Scalar loss value.
        """
        if memory_embeddings is not None and memory_groups is not None:
            return self._compute_xbm_loss(
                embeddings, groups, memory_embeddings, memory_groups
            )

        # Shape: (batch_size, batch_size)
        dists = self.distance_metric.distance_matrix(embeddings)

        if self._mining == "all":
            # Calculate loss for all possible triplets first, then filter by group mask
            # Shape: (batch_size, batch_size, 1)
            anchor_positive_dists = dists.unsqueeze(2)
            # Shape: (batch_size, 1, batch_size)
            anchor_negative_dists = dists.unsqueeze(1)
            # All possible triplets: triplet_loss[anchor_id, positive_id, negative_id]
            # Shape: (batch_size, batch_size, batch_size)
            triplet_loss = anchor_positive_dists - anchor_negative_dists + self._margin

            # set invalid triplets to 0
            mask = get_triplet_mask(groups).float()
            triplet_loss = mask * triplet_loss

            # get rid of easy triplets
            triplet_loss = F.relu(triplet_loss)

            # get the number of triplets with a positive loss
            num_positive_triplets = torch.sum((triplet_loss > 1e-16).float())

            # get scalar loss value
            triplet_loss = torch.sum(triplet_loss) / (num_positive_triplets + 1e-16)

        else:  # batch-hard triplet mining

            # get the hardest positive for each anchor
            anchor_positive_mask = get_anchor_positive_mask(groups).float()
            anchor_positive_dists = (
                anchor_positive_mask * dists
            )  # invalid pairs set to 0
            # Shape: (batch_size,)
            hardest_positive_dists = anchor_positive_dists.max(dim=1)[0]

            # get the hardest negative for each anchor
            anchor_negative_mask = get_anchor_negative_mask(groups).float()
            # add maximum of each row to invalid pairs to make sure not to count loss values from
            # those indices when we apply minimum function later on
            anchor_negative_dists = dists + dists.max(dim=1, keepdim=True)[0] * (
                1.0 - anchor_negative_mask
            )
            hardest_negative_dists = anchor_negative_dists.min(dim=1)[0]

            # combine hardest positives and hardest negatives
            triplet_loss = F.relu(
                # Division by the minimal distance between negative samples scales target distances
                # # and prevents vector collapse
                (hardest_positive_dists - hardest_negative_dists)
                / hardest_negative_dists.mean()
                + self._margin
            )

            # get scalar loss value
            triplet_loss = triplet_loss.mean()

        return triplet_loss

    def _compute_xbm_loss(
        self,
        embeddings: Tensor,
        groups: LongTensor,
        memory_embeddings: Tensor,
        memory_groups: LongTensor,
    ) -> Tensor:
        """Implement XBM loss computation for this loss.

        Args:
            embeddings: shape: (batch_size, vector_length) - Output embeddings from the
                encoder.
            groups: shape: (batch_size,) - Group ids associated with embeddings.
            memory_embeddings: shape: (memory_buffer_size, vector_length) - Embeddings stored
                in a ring buffer
            memory_groups: shape: (memory_buffer_size,) - Groups ids associated with `memory_embeddings`

        Returns:
            Tensor: zero-size tensor, XBM loss value.
        """
        batch_size = embeddings.size(0)

        # Compute similarity matrix
        # shape: (batch_size, memory_buffer_size)
        sim_mat = torch.matmul(embeddings, memory_embeddings.t())

        # split the positive and negative pairs
        # shape: (batch_size, batch_size)
        eyes_ = torch.eye(batch_size, dtype=torch.uint8).to(embeddings.device)

        pos_mask = groups.expand(
            memory_groups.shape[0], batch_size
        ).t() == memory_groups.expand(batch_size, memory_groups.shape[0])
        pos_mask = pos_mask.float()
        neg_mask = 1 - pos_mask
        pos_mask[:, :batch_size] = pos_mask[:, :batch_size] - eyes_

        loss = list()
        neg_count = list()

        for i in range(batch_size):
            pos_pair_idx = torch.nonzero(pos_mask[i, :]).view(-1)
            if pos_pair_idx.shape[0] > 0:
                pos_pair_ = sim_mat[i, pos_pair_idx]
                pos_pair_ = torch.sort(pos_pair_)[0]

                neg_pair_idx = torch.nonzero(neg_mask[i, :]).view(-1)
                neg_pair_ = sim_mat[i, neg_pair_idx]
                neg_pair_ = torch.sort(neg_pair_)[0]

                select_pos_pair_idx = torch.nonzero(
                    pos_pair_ < neg_pair_[-1] + self._margin
                ).view(-1)
                pos_pair = pos_pair_[select_pos_pair_idx]

                select_neg_pair_idx = torch.nonzero(
                    neg_pair_ > max(0.6, pos_pair_[-1]) - self._margin
                ).view(-1)
                neg_pair = neg_pair_[select_neg_pair_idx]

                pos_loss = torch.sum(1 - pos_pair)
                if len(neg_pair) >= 1:
                    neg_loss = torch.sum(neg_pair)
                    neg_count.append(len(neg_pair))
                else:
                    neg_loss = 0
                loss.append(pos_loss + neg_loss)
            else:
                loss.append(0)

        loss = sum(loss) / batch_size

        return loss
