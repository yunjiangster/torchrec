#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

from typing import Any, Dict, List, Optional, Tuple, Union

import torch
import torch.distributed as dist
from torchrec.distributed.dist_data import (
    SeqEmbeddingsAllToOne,
    SequenceEmbeddingsAllToAll,
)
from torchrec.distributed.embedding_lookup import (
    GroupedEmbeddingsLookup,
    InferGroupedEmbeddingsLookup,
)
from torchrec.distributed.embedding_sharding import (
    BaseEmbeddingDist,
    BaseEmbeddingLookup,
    BaseSparseFeaturesDist,
)
from torchrec.distributed.embedding_types import (
    BaseGroupedFeatureProcessor,
    InputDistOutputs,
)
from torchrec.distributed.sharding.rw_sharding import (
    BaseRwEmbeddingSharding,
    get_embedding_shard_metadata,
    InferRwSparseFeaturesDist,
    RwSparseFeaturesDist,
)
from torchrec.distributed.sharding.sequence_sharding import (
    InferSequenceShardingContext,
    SequenceShardingContext,
)
from torchrec.distributed.types import Awaitable, CommOp, QuantizedCommCodecs
from torchrec.sparse.jagged_tensor import KeyedJaggedTensor


class RwSequenceEmbeddingDist(
    BaseEmbeddingDist[SequenceShardingContext, torch.Tensor, torch.Tensor]
):
    """
    Redistributes sequence embedding tensor in RW fashion with an AlltoAll operation.

    Args:
        pg (dist.ProcessGroup): ProcessGroup for AlltoAll communication.
        num_features (int): total number of features.
        device (Optional[torch.device]): device on which buffers will be allocated.
    """

    def __init__(
        self,
        pg: dist.ProcessGroup,
        num_features: int,
        device: Optional[torch.device] = None,
        qcomm_codecs_registry: Optional[Dict[str, QuantizedCommCodecs]] = None,
    ) -> None:
        super().__init__()
        self._dist = SequenceEmbeddingsAllToAll(
            pg,
            [num_features] * pg.size(),
            device,
            codecs=(
                qcomm_codecs_registry.get(
                    CommOp.SEQUENCE_EMBEDDINGS_ALL_TO_ALL.name, None
                )
                if qcomm_codecs_registry
                else None
            ),
        )

    def forward(
        self,
        local_embs: torch.Tensor,
        sharding_ctx: Optional[SequenceShardingContext] = None,
    ) -> Awaitable[torch.Tensor]:
        """
        Performs AlltoAll operation on sequence embeddings tensor.

        Args:
            local_embs (torch.Tensor): tensor of values to distribute.
            sharding_ctx (SequenceShardingContext): shared context from KJTAllToAll
                operation.

        Returns:
            Awaitable[torch.Tensor]: awaitable of sequence embeddings.
        """
        assert sharding_ctx is not None
        return self._dist(
            local_embs,
            lengths=sharding_ctx.lengths_after_input_dist,
            input_splits=sharding_ctx.input_splits,
            output_splits=sharding_ctx.output_splits,
            batch_size_per_rank=sharding_ctx.batch_size_per_rank,
            sparse_features_recat=sharding_ctx.sparse_features_recat,
            unbucketize_permute_tensor=sharding_ctx.unbucketize_permute_tensor,
        )


class RwSequenceEmbeddingSharding(
    BaseRwEmbeddingSharding[
        SequenceShardingContext, KeyedJaggedTensor, torch.Tensor, torch.Tensor
    ]
):
    """
    Shards sequence (unpooled) row-wise, i.e.. a given embedding table is evenly
    distributed by rows and table slices are placed on all ranks.
    """

    def create_input_dist(
        self,
        device: Optional[torch.device] = None,
    ) -> BaseSparseFeaturesDist[KeyedJaggedTensor]:
        num_features = self._get_num_features()
        feature_hash_sizes = self._get_feature_hash_sizes()
        return RwSparseFeaturesDist(
            # pyre-fixme[6]: For 1st param expected `ProcessGroup` but got
            #  `Optional[ProcessGroup]`.
            pg=self._pg,
            num_features=num_features,
            feature_hash_sizes=feature_hash_sizes,
            device=device if device is not None else self._device,
            is_sequence=True,
            has_feature_processor=self._has_feature_processor,
            need_pos=False,
        )

    def create_lookup(
        self,
        device: Optional[torch.device] = None,
        fused_params: Optional[Dict[str, Any]] = None,
        feature_processor: Optional[BaseGroupedFeatureProcessor] = None,
    ) -> BaseEmbeddingLookup:
        return GroupedEmbeddingsLookup(
            grouped_configs=self._grouped_embedding_configs,
            pg=self._pg,
            device=device if device is not None else self._device,
        )

    def create_output_dist(
        self,
        device: Optional[torch.device] = None,
    ) -> BaseEmbeddingDist[SequenceShardingContext, torch.Tensor, torch.Tensor]:
        return RwSequenceEmbeddingDist(
            # pyre-fixme[6]: For 1st param expected `ProcessGroup` but got
            #  `Optional[ProcessGroup]`.
            self._pg,
            self._get_num_features(),
            device if device is not None else self._device,
            qcomm_codecs_registry=self.qcomm_codecs_registry,
        )


class InferRwSequenceEmbeddingDist(
    BaseEmbeddingDist[
        InferSequenceShardingContext, List[torch.Tensor], List[torch.Tensor]
    ]
):
    def __init__(
        self,
        device: torch.device,
        world_size: int,
        device_type_from_sharding_infos: Optional[Union[str, Tuple[str, ...]]] = None,
    ) -> None:
        super().__init__()
        self._dist: SeqEmbeddingsAllToOne = SeqEmbeddingsAllToOne(device, world_size)
        self._device_type_from_sharding_infos: Optional[Union[str, Tuple[str, ...]]] = (
            device_type_from_sharding_infos
        )

    def forward(
        self,
        local_embs: List[torch.Tensor],
        sharding_ctx: Optional[InferSequenceShardingContext] = None,
    ) -> List[torch.Tensor]:
        if self._device_type_from_sharding_infos is not None:
            if isinstance(self._device_type_from_sharding_infos, tuple):
                # Update the tagging when tuple has heterogenous device type
                # Done in next diff stack along with the full support for
                # hetergoenous device type
                return local_embs
            elif self._device_type_from_sharding_infos == "cpu":
                # for cpu sharder, output dist should be a no-op
                return local_embs
        return self._dist(local_embs)


class InferRwSequenceEmbeddingSharding(
    BaseRwEmbeddingSharding[
        InferSequenceShardingContext,
        InputDistOutputs,
        List[torch.Tensor],
        List[torch.Tensor],
    ]
):
    """
    Shards sequence (unpooled) row-wise, i.e.. a given embedding table is evenly
    distributed by rows and table slices are placed on all ranks for inference.
    """

    def create_input_dist(
        self,
        device: Optional[torch.device] = None,
    ) -> BaseSparseFeaturesDist[InputDistOutputs]:
        num_features = self._get_num_features()
        feature_hash_sizes = self._get_feature_hash_sizes()

        (emb_sharding, is_even_sharding) = get_embedding_shard_metadata(
            self._grouped_embedding_configs_per_rank
        )

        return InferRwSparseFeaturesDist(
            world_size=self._world_size,
            num_features=num_features,
            feature_hash_sizes=feature_hash_sizes,
            device=device if device is not None else self._device,
            is_sequence=True,
            has_feature_processor=self._has_feature_processor,
            need_pos=False,
            embedding_shard_metadata=emb_sharding if not is_even_sharding else None,
        )

    def create_lookup(
        self,
        device: Optional[torch.device] = None,
        fused_params: Optional[Dict[str, Any]] = None,
        feature_processor: Optional[BaseGroupedFeatureProcessor] = None,
    ) -> BaseEmbeddingLookup[InputDistOutputs, List[torch.Tensor]]:
        return InferGroupedEmbeddingsLookup(
            grouped_configs_per_rank=self._grouped_embedding_configs_per_rank,
            world_size=self._world_size,
            fused_params=fused_params,
            device=device if device is not None else self._device,
        )

    def create_output_dist(
        self,
        device: Optional[torch.device] = None,
    ) -> BaseEmbeddingDist[
        InferSequenceShardingContext, List[torch.Tensor], List[torch.Tensor]
    ]:
        return InferRwSequenceEmbeddingDist(
            device if device is not None else self._device,
            self._world_size,
            self._device_type_from_sharding_infos,
        )
