# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.

"""Pretrain utilities."""

from datetime import datetime
import math
import sys
import time
import json
from functools import partial
# The earliest we can measure the start time.
_TRAIN_START_TIME = time.time()
import torch
from torch.nn.parallel.distributed import DistributedDataParallel as torchDDP

from megatron import get_args
from megatron import get_signal_handler
from megatron import get_timers
from megatron import get_tokenizer
from megatron import get_tensorboard_writer
from megatron import get_current_global_batch_size
from megatron.utils import get_ltor_masks_and_position_ids
from megatron import get_num_microbatches
from megatron import is_last_rank
from megatron import update_num_microbatches
from megatron.core import mpu, tensor_parallel
from megatron import print_rank_0, is_rank_0
from megatron import print_rank_last
from megatron.checkpointing import load_checkpoint
from megatron.checkpointing import save_checkpoint
from megatron.model import Float16Module
from megatron.model import GPTModel, GPTModelPipe
from megatron.core.enums import ModelType
from megatron.optimizer import get_megatron_optimizer
from megatron.initialize import initialize_megatron
from megatron.initialize import write_args_to_tensorboard
from megatron.initialize import set_jit_fusion_options
from megatron.optimizer_param_scheduler import OptimizerParamScheduler
from megatron.model import DistributedDataParallel as LocalDDP
from megatron.utils import check_adlr_autoresume_termination
from megatron.utils import unwrap_model
from megatron.data.data_samplers import build_pretraining_data_loader
from megatron.utils import calc_params_l2_norm
from megatron.core.pipeline_parallel import get_forward_backward_func
from megatron.core.parallel_state import (
    get_tensor_model_parallel_rank,
    get_tensor_model_parallel_world_size,
    get_tensor_model_parallel_group,
    get_tensor_model_parallel_src_rank,
)
from megatron.core.tensor_parallel.mappings import gather_from_tensor_model_parallel_region
from megatron.utils import report_memory, throughput_calculator, checkpoint_throughput_calculator, update_rotary_pos_emb
from megatron.model.vision.knn_monitor import compute_feature_bank
from megatron.arguments import core_transformer_config_from_args
from megatron.text_generation.sampling import sample

import deepspeed
from deepspeed.runtime.utils import see_memory_usage
from deepspeed.accelerator import get_accelerator
from deepspeed.compression.compress import init_compression, redundancy_clean
from deepspeed.runtime.data_pipeline.data_routing.helper import convert_to_random_ltd
from megatron.model.transformer import ParallelTransformerLayer

from deepspeed import comm as dist

def add_text_generate_args(parser):
    """Text generation arguments."""
    group = parser.add_argument_group(title='text generation')

    group.add_argument("--temperature", type=float, default=1.0,
                       help='Sampling temperature.')
    group.add_argument("--top_p", type=float, default=0.0,
                       help='Top p sampling.')
    group.add_argument("--top_k", type=int, default=0,
                       help='Top k sampling.')
    group.add_argument("--out-seq-length", type=int, default=1024,
                       help='Size of the output generated text.')
    return parser

def tensor_parallel_sample(logits, top_p=0.0, top_k=0, temperature=1.0):

    world_size = get_tensor_model_parallel_world_size()
    rank = get_tensor_model_parallel_rank()
    dst_rank = get_tensor_model_parallel_src_rank()

    if rank == 0:
        tensor_list = [torch.empty_like(logits) for _ in range(world_size)]
    else:
        tensor_list = []
    torch.distributed.gather(logits, tensor_list, dst=dst_rank, group=get_tensor_model_parallel_group())
    
    if rank == 0:
        logits = torch.cat(tensor_list, dim=-1).contiguous()
        new_token = sample(logits, top_p=top_p, top_k=top_k, temperature=temperature)
    else:
        new_token = torch.empty_like(logits[..., 0])

    return new_token

def model_provider(pre_process=True, post_process=True):
    """Build the model."""

    print_rank_0('building GPT model ...')
    see_memory_usage(f"Before Building Model", force=True)

    args = get_args()
    config = core_transformer_config_from_args(args)
    with deepspeed.zero.Init(sequence_data_parallel_group=mpu.get_sequence_data_parallel_group(),
                             remote_device=None if args.remote_device == 'none' else args.remote_device,
                             config_dict_or_path=args.deepspeed_config,
                             enabled=args.zero_stage == 3,
                             mpu=mpu):
        
        if args.deepspeed and not args.no_pipeline_parallel:
            model = GPTModelPipe(
                config=config,
                num_tokentypes=0,
                parallel_output=True,
                sample_fn=partial(tensor_parallel_sample, temperature=args.temperature, top_k=args.top_k, top_p=args.top_p),
            )

            deepspeed.comm.comm.barrier()
            # This is a hack to give us a reference to get_batch_pipe from within training.py
            # We need to call model.set_batch_fn after deepspeed.initialize
            model._megatron_batch_fn = get_batch_pipe

            # Predompute the attention mask and store it in args. This avoids having to
            # pipeline it as an activation during training. The mask is constant, and thus
            # we can reuse it.
            attention_mask = torch.tril(torch.ones(
                (1, args.seq_length, args.seq_length), device=get_accelerator().current_device_name())).view(
                    1, 1, args.seq_length, args.seq_length)

            # Convert attention mask to binary:
            attention_mask = (attention_mask < 0.5)
            if args.fp16:
                attention_mask = attention_mask.half()
            elif args.bf16:
                attention_mask = attention_mask.bfloat16()

            # Convert to bool:
            args.attn_mask = attention_mask.to(torch.bool)

            # For prertaining, since sequence length is fixed, cache rotary embedding in args, to avoid communicating around
            if args.use_rotary_position_embeddings:
                update_rotary_pos_emb(args.seq_length)

        else:
            model = GPTModel(
                config=config,
                num_tokentypes=0,
                parallel_output=True,
                pre_process=pre_process,
                post_process=post_process
            )
    see_memory_usage(f"After Building Model", force=True)
    return model

def _create_ds_config_dict():
    args = get_args()
    if isinstance(args.deepspeed_config, dict) :
        ds_config_dict = args.deepspeed_config
    else:
        with open(args.deepspeed_config, 'r', encoding='utf-8') as config_file:
            ds_config_dict = json.load(config_file)

    if args.universal_checkpoint:
        ds_config_dict["checkpoint"] = {"load_universal": True}

    # Clear config path
    args.deepspeed_config = None 

    return ds_config_dict

def get_batch_pipe(data):
    """Modification of `get_batch` to work on `next(data_iterator)` instead of `data_iterator`"""
    args = get_args()
    tokenizer = get_tokenizer()

    if data['text'].dim() == 3:
        data['text'] = data['text'].squeeze(1)
    # Items and their type.
    keys = ['text']
    datatype = torch.int64

    # Broadcast data.
    data_b = tensor_parallel.broadcast_data(keys, data, datatype)

    # Unpack.
    tokens_ = data_b['text'].long()
    labels = tokens_[:, 1:].contiguous()
    tokens = tokens_[:, :-1].contiguous()

    # Get the masks and postition ids.
    attention_mask, loss_mask, position_ids = get_ltor_masks_and_position_ids(
        tokens,
        tokenizer.eod,
        args.reset_position_ids,
        args.reset_attention_mask,
        args.eod_mask_loss)
    if args.curriculum_learning_legacy and args.curriculum_seqlen < tokens.size()[1]:
        # seqlen-based curriculum learning
        # tokens, position_ids, labels, loss_mask have size [batch size, seqlen]
        tokens = tokens[:, :args.curriculum_seqlen].contiguous()
        position_ids = position_ids[:, :args.curriculum_seqlen].contiguous()
        if labels is not None:
            labels = labels[:, :args.curriculum_seqlen].contiguous()
        loss_mask = loss_mask[:, :args.curriculum_seqlen].contiguous()

    return (tokens, position_ids, attention_mask), (labels, loss_mask)

def setup_model(model_provider_func,model_type):
    """Setup model and optimizer."""
    args = get_args()

    model = get_model(model_provider_func, model_type)

    # initialize the compression here
    unwrapped_model = unwrap_model(model,
                                   (torchDDP, LocalDDP, Float16Module))

    if args.deepspeed:
        args.deepspeed_config_dict = _create_ds_config_dict()
        print_rank_0("DeepSpeed is enabled.")
        model, *_ = deepspeed.initialize(
            model=model[0],
            args=args,
            mpu=mpu if args.no_pipeline_parallel else None,
            config=args.deepspeed_config_dict,
        )
        if isinstance(model, deepspeed.PipelineEngine):
            # hack to get batch_fn from pretrain_gpt.py
            model.set_batch_fn(model.module._megatron_batch_fn)

            assert model.grid.get_pipe_parallel_rank() == mpu.get_pipeline_model_parallel_rank()
            assert model.grid.get_slice_parallel_rank() == mpu.get_tensor_model_parallel_rank()
            assert model.grid.get_data_parallel_rank() == mpu.get_data_parallel_rank()
        model = [model]

    assert args.load
    timers = get_timers()
    timers('load-checkpoint', log_level=0).start(barrier=True)
    args.iteration = load_checkpoint(model, None, None)
    timers('load-checkpoint').stop(barrier=True)
    timers.log(['load-checkpoint'])

    # We only support local DDP with multiple micro-batches.
    if len(model) > 1 or mpu.get_pipeline_model_parallel_world_size() > 1:
        assert args.DDP_impl == 'local'

    # get model without FP16 and/or TorchDDP wrappers
    # if args.iteration == 0 and len(unwrapped_model) == 1 \
    #     and hasattr(unwrapped_model[0], 'init_state_dict_from_bert'):
    #     print_rank_0("Initializing ICT from pretrained BERT model")
    #     unwrapped_model[0].init_state_dict_from_bert()

    # random-LTD requires converting transformer layers
    if args.random_ltd:
        model[0] = convert_to_random_ltd(model[0], ParallelTransformerLayer)

    return model

def get_model(model_provider_func, model_type=ModelType.encoder_or_decoder, wrap_with_ddp=True):
    """Build the model."""
    args = get_args()
    args.model_type = model_type

    # Build model.
    model = model_provider_func()
    model.model_type = model_type

    if not isinstance(model, list):
        model = [model]

    # Disallow training and inference with Transformer Engine
    # for non-GPT models
    args.allow_transformer_engine = all([type(m) == GPTModel for m in model])
    assert args.allow_transformer_engine or args.transformer_impl == 'local', \
        'Transformer Engine is only approved for GPT models'

    # Set tensor model parallel attributes if not set.
    # Only parameters that are already tensor model parallel have these
    # attributes set for them. We should make sure the default attributes
    # are set for all params so the optimizer can use them.
    for model_module in model:
        for param in model_module.parameters():
            tensor_parallel.set_defaults_if_not_set_tensor_model_parallel_attributes(param)

    # Print number of parameters.
    if mpu.get_data_parallel_rank() == 0:
        print(' > number of parameters on (tensor, pipeline) '
              'model parallel rank ({}, {}): {}'.format(
            mpu.get_tensor_model_parallel_rank(),
            mpu.get_pipeline_model_parallel_rank(),
            sum([sum([p.ds_numel if hasattr(p,'ds_id') else p.nelement() for p in model_module.parameters()])
                 for model_module in model])), flush=True)

    return model

def main(args_defaults = None):
    initialize_megatron(extra_args_provider=add_text_generate_args,args_defaults=args_defaults)
    args = get_args()
    tokenizer = get_tokenizer()
    data = [{"text":"This can be achieved by directly using the LlamaTokenizer class, or passing in a b c"},
            {"text":"Of cause, I'm not a fan of the new movie. It's too bad that"},
            {"text":"As any dog owner knows, our furry little friends can sometimes be a lot to handle. But there's absolutely nothing that excuses what one owner from Chengdu, China, did to their six-week-old pup.\nIt all started when the adorable puppy, who is now known as Tuffy, accidentally mistook his former owner's phone for a chew toy—as a young pup often does. But instead of being understanding, the angry owner doused Tuffy with boiling hot water and threw him off a"},
            ]
    inputs_ids = list(map(lambda dict: {"text": torch.tensor([tokenizer.tokenize(dict['text'])])}, data))
    data_loader = build_pretraining_data_loader(inputs_ids, args.consumed_train_samples)
    data_iter = iter(data_loader)
    model = setup_model(model_provider_func=model_provider, model_type=ModelType.encoder_or_decoder)
    if dist.get_rank() == 0:
        import madbg; madbg.set_trace(ip='0.0.0.0', port=8888 + dist.get_rank())
    model[0].generate_batch(data_iter)

if __name__ == "__main__":
    main(args_defaults={'tokenizer_type': 'HFTokenizer'})
    # main(args_defaults={'tokenizer_type': 'GPT2BPETokenizer'})