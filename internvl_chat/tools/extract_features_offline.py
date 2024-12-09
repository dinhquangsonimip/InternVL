# --------------------------------------------------------
# InternVL
# Copyright (c) 2024 OpenGVLab
# Licensed under The MIT License [see LICENSE for details]
# --------------------------------------------------------

import logging
import math
import os
import random
import shutil
import sys
import traceback
import warnings
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, Literal, Optional

import numpy as np

try:
    import orjson as json
except:
    import json

import torch
import torch.distributed as dist
import transformers
from internvl.dist_utils import init_dist
from internvl.model.internlm2.modeling_internlm2 import InternLM2ForCausalLM
from internvl.model.internvl_chat import (InternVisionConfig,
                                          InternVisionModel,
                                          InternVLChatConfig,
                                          InternVLChatModel)
from internvl.patch import (concat_pad_data_collator,
                            dpo_concat_pad_data_collator,
                            replace_llama_rmsnorm_with_fused_rmsnorm,
                            replace_train_sampler)
from internvl.train.constants import (BOX_END_TOKEN, BOX_START_TOKEN,
                                      IMG_CONTEXT_TOKEN, IMG_END_TOKEN,
                                      IMG_START_TOKEN, QUAD_END_TOKEN,
                                      QUAD_START_TOKEN, REF_END_TOKEN,
                                      REF_START_TOKEN)
from internvl.train.dataset import (ConcatDataset, TCSLoader,
                                    WeightedConcatDataset, build_transform,
                                    dynamic_preprocess, preprocess,
                                    preprocess_internlm,
                                    preprocess_internvl2_5, preprocess_mpt,
                                    preprocess_internlm_v3,
                                    preprocess_phi3)
from internvl.train.trainer_dpo import MultimodalDPOTrainer
from PIL import Image, ImageFile, PngImagePlugin, UnidentifiedImageError
from torch.utils.data import Dataset
from transformers import (AutoConfig, AutoModelForCausalLM, AutoTokenizer,
                          HfArgumentParser, Trainer, TrainingArguments,
                          set_seed)
from transformers.trainer_utils import get_last_checkpoint
from transformers.utils.logging import (enable_default_handler,
                                        enable_explicit_format, set_verbosity)
from trl import DPOConfig as DPOConfigTRL

# Try to import petrel_client for image loading, fallback to PIL if unavailable
try:
    from petrel_client.client import Client
    from petrel_client.common.config import Config
    has_tcs_loader = True
except ImportError as E:
    print('petrel_client is not installed. Using PIL to load images.')
    has_tcs_loader = False

# Set constants for image processing and logging
IGNORE_INDEX = -100
Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = True
MaximumDecompressedSize = 1024
MegaByte = 2 ** 20
PngImagePlugin.MAX_TEXT_CHUNK = MaximumDecompressedSize * MegaByte

warnings.filterwarnings('ignore')
logger = logging.getLogger(__name__)

os.environ['TOKENIZERS_PARALLELISM'] = 'true'


@dataclass
class ModelArguments:
    """
    Arguments for specifying model, tokenizer, and configurations.
    """
    model_name_or_path: Optional[str] = field(
        default=None,
        metadata={'help': 'Path to a pretrained model (local or from huggingface.co/models).'}
    )
    vision_path: Optional[str] = field(
        default=None,
        metadata={'help': 'Path to a pretrained model (local or from huggingface.co/models).'}
    )
    llm_path: Optional[str] = field(
        default=None,
        metadata={'help': 'Path to a pretrained model (local or from huggingface.co/models).'}
    )
    mlp_path: Optional[str] = field(
        default=None,
        metadata={'help': 'Path to a pretrained model (local or from huggingface.co/models).'}
    )
    freeze_llm: bool = field(
        default=False,
        metadata={'help': 'Set to True to freeze the LLM. Default is False.'},
    )
    freeze_backbone: bool = field(
        default=False,
        metadata={'help': 'Set to True to freeze the ViT. Default is False.'},
    )
    freeze_mlp: bool = field(
        default=False,
        metadata={'help': 'Set to True to freeze the MLP. Default is False.'},
    )
    unfreeze_vit_layers: int = field(
        default=0,
        metadata={'help': 'Specify the number of ViT layers to unfreeze. Default is 0.'},
    )
    vision_select_layer: int = field(
        default=-1,
        metadata={'help': 'Specify the layer of ViT feature map to use. Default is -1 for the last layer.'},
    )
    use_backbone_lora: int = field(
        default=0,
        metadata={'help': 'Set the LoRA adapter rank for the ViT. Default is 0.'}
    )
    use_llm_lora: int = field(
        default=0,
        metadata={'help': 'Set the LoRA adapter rank for the LLM. Default is 0.'}
    )
    unfreeze_lm_head: bool = field(
        default=False,
        metadata={'help': 'Set to True to unfreeze the head of LLM. Default is False.'},
    )
    grad_checkpoint: bool = field(
        default=True,
        metadata={'help': 'Set to True to use gradient checkpointing. Default is True.'},
    )
    drop_path_rate: float = field(
        default=0.0,
        metadata={'help': 'Set the drop path rate for the ViT. Default is 0.'},
    )
    ps_version: Literal['v1', 'v2'] = field(
        default='v2',
        metadata={'help': 'Specify the version of pixel shuffle implementation. Default is v2.'}
    )
    use_fast_tokenizer: bool = field(
        default=False,
        metadata={'help': 'Set to True to use the fast mode of the tokenizer.'}
    )
    use_liger: bool = field(
        default=False,
        metadata={'help': 'Set to True to use the liger kernel.'}
    )


@dataclass
class DataTrainingArguments:
    """
    Arguments for specifying data input for training and evaluation.
    """
    max_seq_length: int = field(
        default=8192,
        metadata={
            'help': (
                'The maximum total input sequence length after tokenization. Sequences longer '
                'than this will be truncated, sequences shorter will be padded.'
            )
        },
    )
    force_image_size: int = field(
        default=448,
        metadata={'help': 'Set the desired size for the image. Default is 448.'},
    )
    down_sample_ratio: float = field(
        default=0.5,
        metadata={'help': 'Set the desired down-sampling ratio for the image. Default is 0.5.'},
    )
    pad2square: bool = field(
        default=False,
        metadata={'help': 'Pad the image to a square shape if set to True. Default is False.'},
    )
    conv_style: str = field(
        default='internlm2-chat', metadata={'help': 'Prompt style for a conversation.'}
    )
    meta_path: str = field(
        default=None,
        metadata={'help': 'The path of the meta file of datasets.'},
    )
    use_data_resampling: bool = field(
        default=False,
        metadata={'help': 'Set to True to use data resampling. Default is False.'},
    )
    dynamic_image_size: bool = field(
        default=False,
        metadata={'help': 'Set to True to use dynamic high resolution strategy. Default is False.'},
    )
    use_thumbnail: bool = field(
        default=False,
        metadata={'help': 'Set to True to add a thumbnail image. Default is False.'},
    )
    min_dynamic_patch: int = field(
        default=1,
        metadata={'help': 'The minimum number of dynamic patches. Default is 1.'},
    )
    max_dynamic_patch: int = field(
        default=12,
        metadata={'help': 'The maximum number of dynamic patches. Default is 12.'},
    )
    min_num_frame: int = field(
        default=8,
        metadata={'help': 'The minimum number of frames for video data. Default is 8.'},
    )
    max_num_frame: int = field(
        default=32,
        metadata={'help': 'The maximum number of frames for video data. Default is 32.'},
    )
    normalize_type: Literal['imagenet', 'clip', 'siglip'] = field(
        default='imagenet',
        metadata={'help': 'The normalization type for the image. Default is imagenet.'},
    )
    sigmoid_loss_weight: float = field(
        default=1.0,
        metadata={'help': 'Loss weight for DPO loss. Default is 1.0'},
    )
    bco_pair_loss_weight: float = field(
        default=1.0,
        metadata={'help': 'Loss weight for BCO loss. Default is 1.0'},
    )


class DPOConfig(DPOConfigTRL):
    loss_type: Literal[
        'sigmoid', 'hinge', 'ipo', 'bco_pair', 'sppo_hard', 'nca_pair', 'robust', 'aot', 'aot_pair', 'exo_pair',
        'sigmoid,bco_pair',
    ] = 'sigmoid'


class LazySupervisedDataset(Dataset):
    """Dataset for supervised fine-tuning."""

    def __init__(
        self,
        template_name,
        meta,
        tokenizer,
        tcs_loader,
        ds_name,
        num_image_token,
        image_size=448,
        is_train=True,
        pad2square=False,
        group_by_length=False,
        dynamic_image_size=False,
        use_thumbnail=False,
        min_dynamic_patch=1,
        max_dynamic_patch=12,
        min_num_frame=8,  # for video data
        max_num_frame=32,  # for video data
        sampling_method='rand',  # for video data
        repeat_time=1,
        normalize_type='imagenet',
        random_seed=0,
    ):
        super(LazySupervisedDataset, self).__init__()
        self.ds_name = ds_name
        self.tokenizer = tokenizer
        self.template_name = template_name
        self.num_image_token = num_image_token
        logger.info(f'[Dataset] num_image_token: {num_image_token}')
        logger.info(f'[Dataset] dynamic_image_size: {dynamic_image_size}')
        logger.info(f'[Dataset] use_thumbnail: {use_thumbnail}')
        logger.info(f'[Dataset] min_dynamic_patch: {min_dynamic_patch}, max_dynamic_patch: {max_dynamic_patch}')

        self.image_size = image_size
        self.is_train = is_train
        self.pad2square = pad2square
        self.max_num_frame = max_num_frame
        self.min_num_frame = min_num_frame
        self.sampling_method = sampling_method
        self.conv_end_token_id = tokenizer.convert_tokens_to_ids('<|im_end|>')

        logger.info('Formatting inputs...Skip in lazy mode')
        assert meta['annotation'].endswith('jsonl'), f'annotation must be jsonl, but got {meta["annotation"]}'

        with open(meta['annotation'], 'r') as f:
            self.raw_data = f.readlines()
            # if repeat_time < 1:
            #     # If repeat_time is less than 1, select a portion of the data
            #     self.raw_data = random.sample(self.raw_data, k=int(len(self.raw_data) * repeat_time))
            # if repeat_time > 1:
            #     repeat_time = int(repeat_time)
            #     assert isinstance(repeat_time, int)
            #     # Repeat the list if repeat_time is greater than 1
            #     self.raw_data = self.raw_data * repeat_time

        self.rng = np.random.default_rng(seed=random_seed)
        self.rng.shuffle(self.raw_data)

        self.root = meta['root']
        self.cached_data_dict = {}
        self.tcs_loader = tcs_loader
        self.group_by_length = group_by_length
        self.dynamic_image_size = dynamic_image_size
        self.use_thumbnail = use_thumbnail
        self.min_dynamic_patch = min_dynamic_patch
        self.max_dynamic_patch = max_dynamic_patch
        self.normalize_type = normalize_type

        # If the precomputed length does not exist, roughly estimate the length of
        # each sample to improve the efficiency of group_by_length.
        if self.group_by_length:
            self.conv2length = {}  # Using a dictionary to speed up token length calculation
            self.length = []
            for data_item in self.raw_data:
                data_item = json.loads(data_item)
                if 'length' in data_item:
                    token_length = data_item['length']  # Use precomputed length if available
                else:
                    # Compute token length using the tokenizer
                    conversations = '\n'.join([temp['value'] for temp in data_item['conversations']])
                    str_length = len(conversations)
                    if str_length not in self.conv2length:
                        token_length = tokenizer(
                            conversations, return_tensors='pt', padding=False, truncation=False,
                        ).input_ids.size(1)
                        self.conv2length[str_length] = token_length + num_image_token * (
                                    max_dynamic_patch + use_thumbnail)
                    else:
                        token_length = self.conv2length[str_length]
                self.length.append(token_length)

    def __len__(self):
        return len(self.raw_data)

    def get_preprocess_function(self):
        # Select the appropriate preprocessing function based on the template name
        if self.template_name == 'Hermes-2':
            preprocess_function = preprocess_mpt
        elif self.template_name == 'internlm2-chat':
            preprocess_function = preprocess_internlm
        elif self.template_name == 'phi3-chat':
            preprocess_function = preprocess_phi3
        elif self.template_name == 'internvl2_5':
            preprocess_function = preprocess_internvl2_5
        elif self.template_name == 'internlm2-chat-v3':
            preprocess_function = preprocess_internlm_v3
        else:
            preprocess_function = preprocess
        return preprocess_function

    def load_image(self, image_path):
        # Load the image using tcs_loader if available, otherwise use PIL
        if self.tcs_loader is not None and 's3://' in image_path:
            return self.tcs_loader(image_path)
        return Image.open(image_path).convert('RGB')

    def get_image_path(self, image_path):
        if image_path.startswith('s3://'):  # for ceph
            image_path = self.root + image_path
        else:  # for local image
            image_path = os.path.join(self.root, image_path)
        return image_path

    def get_transform(self):
        # Build transformation function
        transform = build_transform(is_train=self.is_train, input_size=self.image_size,
                                    pad2square=self.pad2square, normalize_type=self.normalize_type)
        return transform

    @staticmethod
    def get_longest_common_prefix_index(tensor1, tensor2):
        min_len = min(len(tensor1), len(tensor2))

        for i in range(min_len):
            if tensor1[i] != tensor2[i]:
                return i

        return min_len

    def multi_modal_get_item(self, data_item):
        # Build transformation function
        transform = self.get_transform()

        # Ensure the first conversation contains an image placeholder
        if '<image>' not in data_item['question']:
            data_item['question'] = '<image>\n' + data_item['question']

        # Merge the image path
        image_path = self.get_image_path(data_item['image'])

        # Load the image using tcs_loader if available, otherwise use PIL
        image = self.load_image(image_path)

        if self.dynamic_image_size:  # If dynamic image size is enabled, preprocess the image dynamically
            images = dynamic_preprocess(image, min_num=self.min_dynamic_patch, max_num=self.max_dynamic_patch,
                                        image_size=self.image_size, use_thumbnail=self.use_thumbnail)
        else:  # Otherwise, use the original image as a single patch
            images = [image]

        # Apply the transformation to each image and stack the results into a tensor
        pixel_values = [transform(image) for image in images]
        pixel_values = torch.stack(pixel_values)

        # Ensure that there is only one patch if dynamic image size is not enabled
        num_patches = pixel_values.size(0)
        if not self.dynamic_image_size:
            assert num_patches == 1, f'The number of patches should be 1, but got {num_patches}.'

        # Select the appropriate preprocessing function based on the template name
        preprocess_function = self.get_preprocess_function()

        # Preprocess the conversations and generate the return dictionary
        chosen_conversations = [
            {'from': 'human', 'value': data_item['question']},
            {'from': 'gpt', 'value': data_item['chosen']},
        ]
        chosen_ret = preprocess_function(
            self.template_name,
            [deepcopy(chosen_conversations)],
            self.tokenizer,
            [self.num_image_token * num_patches],
            group_by_length=True,
            ds_name=self.ds_name,
        )

        rejected_conversations = [
            {'from': 'human', 'value': data_item['question']},
            {'from': 'gpt', 'value': data_item['rejected']},
        ]
        rejected_ret = preprocess_function(
            self.template_name,
            [deepcopy(rejected_conversations)],
            self.tokenizer,
            [self.num_image_token * num_patches],
            group_by_length=True,
            ds_name=self.ds_name,
        )

        process_supervision = data_item.get('process_supervision', False)

        if process_supervision:
            assert (chosen_ret['input_ids'][:, -2] == self.conv_end_token_id).all(), (
                f"\n{chosen_ret['input_ids']}\n\n"
                f"{chosen_ret['input_ids'][:, -2]}\n\n"
                f"{self.conv_end_token_id}"
            )
            chosen_ret['input_ids'] = chosen_ret['input_ids'][:,:-2]
            chosen_ret['labels'] = chosen_ret['labels'][:,:-2]
            chosen_ret['attention_mask'] = chosen_ret['attention_mask'][:,:-2]

            assert (rejected_ret['input_ids'][:, -2] == self.conv_end_token_id).all()
            rejected_ret['input_ids'] = rejected_ret['input_ids'][:,:-2]
            rejected_ret['labels'] = rejected_ret['labels'][:,:-2]
            rejected_ret['attention_mask'] = rejected_ret['attention_mask'][:,:-2]

        # Create the final return dictionary
        ret = dict(
            chosen_input_ids=chosen_ret['input_ids'][0],
            chosen_labels=chosen_ret['labels'][0],
            chosen_attention_mask=chosen_ret['attention_mask'][0],
            rejected_input_ids=rejected_ret['input_ids'][0],
            rejected_labels=rejected_ret['labels'][0],
            rejected_attention_mask=rejected_ret['attention_mask'][0],
            pixel_values=pixel_values,
            image_flags=torch.tensor([1] * num_patches, dtype=torch.long),
            process_supervision=process_supervision,
        )
        return ret

    def multi_modal_multi_image_get_item(self, data_item):
        # Build transformation function
        transform = self.get_transform()

        images, num_tiles = [], []
        num_image = len(data_item['image'])
        for image_path in data_item['image']:
            # Merge the image path
            image_path = self.get_image_path(image_path)
            # Load the image using tcs_loader if available, otherwise use PIL
            image = self.load_image(image_path)
            if self.dynamic_image_size:  # If dynamic image size is enabled, preprocess the image dynamically
                image = dynamic_preprocess(image, min_num=self.min_dynamic_patch,
                                           max_num=max(1, self.max_dynamic_patch // num_image),
                                           image_size=self.image_size, use_thumbnail=self.use_thumbnail)
                images += image
                num_tiles.append(len(image))
            else:  # Otherwise, use the original image as a single patch
                images.append(image)
                num_tiles.append(1)
        pixel_values = [transform(image) for image in images]
        pixel_values = torch.stack(pixel_values)
        num_patches = pixel_values.size(0)

        # Select the appropriate preprocessing function based on the template name
        preprocess_function = self.get_preprocess_function()

        # Preprocess the conversations and generate the return dictionary
        num_image_tokens = [self.num_image_token * num_tile for num_tile in num_tiles]

        chosen_conversations = [
            {'from': 'human', 'value': data_item['question']},
            {'from': 'gpt', 'value': data_item['chosen']},
        ]
        chosen_ret = preprocess_function(
            self.template_name,
            [deepcopy(chosen_conversations)],
            self.tokenizer,
            num_image_tokens,
            group_by_length=self.group_by_length,
            ds_name=self.ds_name,
            num_image=num_image,
        )

        rejected_conversations = [
            {'from': 'human', 'value': data_item['question']},
            {'from': 'gpt', 'value': data_item['rejected']},
        ]
        rejected_ret = preprocess_function(
            self.template_name,
            [deepcopy(rejected_conversations)],
            self.tokenizer,
            num_image_tokens,
            group_by_length=self.group_by_length,
            ds_name=self.ds_name,
            num_image=num_image,
        )

        process_supervision = data_item.get('process_supervision', False)

        if process_supervision:
            assert (chosen_ret['input_ids'][:, -2] == self.conv_end_token_id).all()
            chosen_ret['input_ids'] = chosen_ret['input_ids'][:,:-2]
            chosen_ret['labels'] = chosen_ret['labels'][:,:-2]
            chosen_ret['attention_mask'] = chosen_ret['attention_mask'][:,:-2]

            assert (rejected_ret['input_ids'][:, -2] == self.conv_end_token_id).all()
            rejected_ret['input_ids'] = rejected_ret['input_ids'][:,:-2]
            rejected_ret['labels'] = rejected_ret['labels'][:,:-2]
            rejected_ret['attention_mask'] = rejected_ret['attention_mask'][:,:-2]

        # Create the final return dictionary
        ret = dict(
            chosen_input_ids=chosen_ret['input_ids'][0],
            chosen_labels=chosen_ret['labels'][0],
            chosen_attention_mask=chosen_ret['attention_mask'][0],
            rejected_input_ids=rejected_ret['input_ids'][0],
            rejected_labels=rejected_ret['labels'][0],
            rejected_attention_mask=rejected_ret['attention_mask'][0],
            pixel_values=pixel_values,
            image_flags=torch.tensor([1] * num_patches, dtype=torch.long),
            process_supervision=process_supervision,
        )
        return ret

    def video_get_item(self, data_item):
        # Build transformation function
        transform = self.get_transform()

        # Ensure the first conversation contains a video placeholder
        if '<video>' not in data_item['question']:
            data_item['question'] = '<video>\n' + data_item['question']

        # Get the video file path
        video_file = data_item['video']
        video_path = os.path.join(self.root, video_file)

        # Load the video frames using tcs_loader
        # TODO: Load videos without using tcsloader.
        image_list = self.tcs_loader(
            video_path,
            image_type='video',
            max_num_frames=self.max_num_frame,
            min_num_frames=self.min_num_frame,
            sample=self.sampling_method,
            clip=data_item.get('clip', None))

        # Generate special tokens for each video frame
        special_tokens = '\n'.join(['Frame{}: <image>'.format(i + 1) for i in range(len(image_list))])
        data_item['question'] = data_item['question'].replace('<video>\n', special_tokens)

        # Transform each frame image and stack them into a tensor
        pixel_values = [transform(image) for image in image_list]
        pixel_values = torch.stack(pixel_values)
        num_patches = pixel_values.size(0)

        # Select the appropriate preprocessing function based on the template name
        preprocess_function = self.get_preprocess_function()

        # Preprocess the conversations and generate the return dictionary
        num_image_tokens = [self.num_image_token] * num_patches

        chosen_conversations = [
            {'from': 'human', 'value': data_item['question']},
            {'from': 'gpt', 'value': data_item['chosen']},
        ]
        chosen_ret = preprocess_function(
            self.template_name,
            [deepcopy(chosen_conversations)],
            self.tokenizer,
            num_image_tokens,
            group_by_length=True,
            use_packed_ds=self.use_packed_ds,
            ds_name=self.ds_name,
            num_image=num_patches,
        )

        rejected_conversations = [
            {'from': 'human', 'value': data_item['question']},
            {'from': 'gpt', 'value': data_item['rejected']},
        ]
        rejected_ret = preprocess_function(
            self.template_name,
            [deepcopy(rejected_conversations)],
            self.tokenizer,
            num_image_tokens,
            group_by_length=True,
            use_packed_ds=self.use_packed_ds,
            ds_name=self.ds_name,
            num_image=num_patches,
        )

        process_supervision = data_item.get('process_supervision', False)

        if process_supervision:
            assert (chosen_ret['input_ids'][:, -2] == self.conv_end_token_id).all()
            chosen_ret['input_ids'] = chosen_ret['input_ids'][:,:-2]
            chosen_ret['labels'] = chosen_ret['labels'][:,:-2]
            chosen_ret['attention_mask'] = chosen_ret['attention_mask'][:,:-2]

            assert (rejected_ret['input_ids'][:, -2] == self.conv_end_token_id).all()
            rejected_ret['input_ids'] = rejected_ret['input_ids'][:,:-2]
            rejected_ret['labels'] = rejected_ret['labels'][:,:-2]
            rejected_ret['attention_mask'] = rejected_ret['attention_mask'][:,:-2]

        ret = dict(
            chosen_input_ids=chosen_ret['input_ids'][0],
            chosen_labels=chosen_ret['labels'][0],
            chosen_attention_mask=chosen_ret['attention_mask'][0],
            rejected_input_ids=rejected_ret['input_ids'][0],
            rejected_labels=rejected_ret['labels'][0],
            rejected_attention_mask=rejected_ret['attention_mask'][0],
            pixel_values=pixel_values,
            image_flags=torch.tensor([1] * num_patches, dtype=torch.long),
            process_supervision=process_supervision,
        )
        return ret

    def pure_text_get_item(self, data_item):
        # Build transformation function
        transform = self.get_transform()

        # Create a blank white image
        image = Image.new('RGB', (224, 224), (255, 255, 255))

        # Dynamically preprocess the image to generate patches
        images = dynamic_preprocess(image, min_num=self.min_dynamic_patch, max_num=1,
                                    image_size=self.image_size, use_thumbnail=self.use_thumbnail)

        # Apply the transformation to each image patch and stack them into a tensor
        pixel_values = [transform(image) for image in images]
        pixel_values = torch.stack(pixel_values)
        num_patches = pixel_values.size(0)

        # Ensure there is only one patch
        assert num_patches == 1, f'The number of patches should be 1, but got {num_patches}.'

        # Select the appropriate preprocessing function based on the template name
        preprocess_function = self.get_preprocess_function()

        # Preprocess the conversations and generate the return dictionary
        chosen_conversations = [
            {'from': 'human', 'value': data_item['question']},
            {'from': 'gpt', 'value': data_item['chosen']},
        ]
        chosen_ret = preprocess_function(
            self.template_name,
            [deepcopy(chosen_conversations)],
            self.tokenizer,
            [self.num_image_token * num_patches],
            text_only=True,
            group_by_length=True,
            ds_name=self.ds_name,
        )

        rejected_conversations = [
            {'from': 'human', 'value': data_item['question']},
            {'from': 'gpt', 'value': data_item['rejected']},
        ]
        rejected_ret = preprocess_function(
            self.template_name,
            [deepcopy(rejected_conversations)],
            self.tokenizer,
            [self.num_image_token * num_patches],
            text_only=True,
            group_by_length=True,
            ds_name=self.ds_name,
        )

        process_supervision = data_item.get('process_supervision', False)

        if process_supervision:
            assert (chosen_ret['input_ids'][:, -2] == self.conv_end_token_id).all()
            chosen_ret['input_ids'] = chosen_ret['input_ids'][:,:-2]
            chosen_ret['labels'] = chosen_ret['labels'][:,:-2]
            chosen_ret['attention_mask'] = chosen_ret['attention_mask'][:,:-2]

            assert (rejected_ret['input_ids'][:, -2] == self.conv_end_token_id).all()
            rejected_ret['input_ids'] = rejected_ret['input_ids'][:,:-2]
            rejected_ret['labels'] = rejected_ret['labels'][:,:-2]
            rejected_ret['attention_mask'] = rejected_ret['attention_mask'][:,:-2]

        # Create the final return dictionary
        ret = dict(
            chosen_input_ids=chosen_ret['input_ids'][0],
            chosen_labels=chosen_ret['labels'][0],
            chosen_attention_mask=chosen_ret['attention_mask'][0],
            rejected_input_ids=rejected_ret['input_ids'][0],
            rejected_labels=rejected_ret['labels'][0],
            rejected_attention_mask=rejected_ret['attention_mask'][0],
            pixel_values=pixel_values,
            image_flags=torch.tensor([0] * num_patches, dtype=torch.long),
            process_supervision=process_supervision,
        )
        return ret

    def __getitem__(self, i) -> Dict[str, torch.Tensor]:
        i = i % len(self.raw_data)

        try_cnt, max_try = 0, 10
        while True:
            if try_cnt > max_try:
                raise StopIteration
            try:
                data_item = json.loads(self.raw_data[i])
                if 'image' in data_item and len(data_item['image']) != 0:
                    if type(data_item['image']) == list:
                        ret = self.multi_modal_multi_image_get_item(data_item)
                    else:
                        ret = self.multi_modal_get_item(data_item)
                elif 'video' in data_item and data_item['video'] is not None and data_item['video'] != '':
                    ret = self.video_get_item(data_item)
                else:
                    ret = self.pure_text_get_item(data_item)
                break
            except Exception as e:
                try_cnt += 1
                print(e, self.ds_name, flush=True)
                if not isinstance(e, (UnidentifiedImageError, FileNotFoundError)):
                    traceback.print_exc()
                data_item = json.loads(self.raw_data[i])
                if 'image' in data_item:
                    if type(data_item['image']) == list:
                        images = [self.root + item for item in data_item['image']]
                        print(f'Failed to load image: {images}, the dataset is: {self.ds_name}')
                    else:
                        if data_item['image'].startswith('s3://'):
                            data_path = self.root + data_item['image']
                        else:
                            data_path = os.path.join(self.root, data_item['image'])
                        print(f'Failed to load image: {data_path}, the dataset is: {self.ds_name}')
                elif 'video' in data_item:
                    data_path = os.path.join(self.root, data_item['video'])
                    print(f'Failed to load video: {data_path}, the dataset is: {self.ds_name}')
                i = random.randint(0, len(self.raw_data) - 1)
        return ret


def build_datasets(
    data_args,
    tokenizer,
    tcs_loader,
    model,
    group_by_length=False,
    dynamic_image_size=False,
    use_thumbnail=False,
    min_dynamic_patch=1,
    max_dynamic_patch=12,
    min_num_frame=8,
    max_num_frame=32,
    normalize_type='imagenet',
):
    datasets = []
    lengths = []
    ds_collections = json.loads(open(data_args.meta_path).read())
    for ds_idx, ds_name in enumerate(ds_collections.keys()):
        repeat_time = ds_collections[ds_name]['repeat_time']
        if 'max_dynamic_patch' in ds_collections[ds_name]:
            max_num = ds_collections[ds_name]['max_dynamic_patch']
            logger.info(f'max_dynamic_patch is set to {max_num} according to the meta file')
        else:
            max_num = max_dynamic_patch
        dataset = LazySupervisedDataset(
            data_args.conv_style, ds_collections[ds_name],
            tokenizer,
            tcs_loader,
            ds_name=ds_name,
            num_image_token=model.num_image_token,
            image_size=data_args.force_image_size,
            is_train=ds_collections[ds_name].get('data_augment', False),
            pad2square=data_args.pad2square,
            group_by_length=group_by_length,
            dynamic_image_size=dynamic_image_size,
            use_thumbnail=use_thumbnail,
            min_dynamic_patch=min_dynamic_patch,
            max_dynamic_patch=max_num,
            min_num_frame=min_num_frame,
            max_num_frame=max_num_frame,
            repeat_time=repeat_time,
            normalize_type=normalize_type,
            random_seed=ds_idx,
        )
        logger.info(f'Add dataset: {ds_name} with length: {len(dataset)}')
        datasets.append(dataset)
        if data_args.use_data_resampling:
            lengths.append(math.sqrt(len(dataset)))
        else:
            lengths.append(len(dataset))

    if data_args.use_data_resampling:
        total_length = sum(lengths)
        weights = [l / total_length for l in lengths]
        train_dataset = WeightedConcatDataset(datasets, weights)
    else:
        train_dataset = ConcatDataset(datasets)
    return train_dataset


def main():
    # Apply necessary patches for the transformers library
    replace_llama_rmsnorm_with_fused_rmsnorm()
    replace_train_sampler()

    # Parse input arguments
    # See all possible arguments in src/transformers/training_args.py
    # If use DeepSpeed zero3, init_dist must before HfArgumentParser
    launcher = os.environ.get('LAUNCHER', 'slurm')
    init_dist(launcher=launcher, backend='nccl')
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, DPOConfig))
    if len(sys.argv) == 2 and sys.argv[1].endswith('.json'):
        # If we pass only one argument to the script, and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args = parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    training_args.remove_unused_columns = False
    training_args.gradient_checkpointing = model_args.grad_checkpoint
    training_args.sigmoid_loss_weight = data_args.sigmoid_loss_weight
    training_args.bco_pair_loss_weight = data_args.bco_pair_loss_weight

    # Sending telemetry. Tracking the example usage helps us better allocate resources to maintain them. The
    # information sent is the one passed as arguments along with your Python/PyTorch versions.
    # send_example_telemetry('InternV-Chat', model_args, data_args)

    # Setup logging
    logging.basicConfig(
        format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
        datefmt='%m/%d/%Y %H:%M:%S',
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # if training_args.should_log:
    #     # The default of training_args.log_level is passive, so we set log level at info here to have that default.
    #     transformers.utils.logging.set_verbosity_info()

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    set_verbosity(log_level)
    enable_default_handler()
    enable_explicit_format()

    # Log on each process the small summary:
    logger.warning(
        f'Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}'
        + f'distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}'
    )
    logger.info(f'Training/evaluation parameters {training_args}')

    # Detecting last checkpoint and eventually continue from last checkpoint.
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
            raise ValueError(
                f'Output directory ({training_args.output_dir}) already exists and is not empty. '
                'Use --overwrite_output_dir to overcome.'
            )
        elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
            logger.info(
                f'Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change '
                'the `--output_dir` or add `--overwrite_output_dir` to train from scratch.'
            )
    # Set seed before initializing model.
    set_seed(training_args.seed)

    # Load pretrained model, tokenizer, and image processor
    tokenizer_path = model_args.model_name_or_path or model_args.llm_path
    logger.info(f'Loading Tokenizer: {tokenizer_path}')
    tokenizer = AutoTokenizer.from_pretrained(
        tokenizer_path, add_eos_token=False, trust_remote_code=True, use_fast=model_args.use_fast_tokenizer)
    tokenizer.tokenizer_path = tokenizer_path
    tokenizer.model_max_length = data_args.max_seq_length
    token_list = [IMG_START_TOKEN, IMG_END_TOKEN, IMG_CONTEXT_TOKEN,
                  QUAD_START_TOKEN, QUAD_END_TOKEN, REF_START_TOKEN,
                  REF_END_TOKEN, BOX_START_TOKEN, BOX_END_TOKEN]
    num_new_tokens = tokenizer.add_tokens(token_list, special_tokens=True)
    img_context_token_id = tokenizer.convert_tokens_to_ids(IMG_CONTEXT_TOKEN)
    tcs_loader = TCSLoader('~/petreloss.conf') if has_tcs_loader else None

    if model_args.use_liger:
        from internvl.patch import apply_liger_kernel_to_internvit
        from liger_kernel.transformers import (apply_liger_kernel_to_llama,
                                               apply_liger_kernel_to_qwen2)
        apply_liger_kernel_to_llama()
        apply_liger_kernel_to_qwen2()
        # apply_liger_kernel_to_internvit()

    assert model_args.model_name_or_path is not None

    logger.info('Loading InternVLChatModel...')
    config = InternVLChatConfig.from_pretrained(model_args.model_name_or_path)
    config.vision_config.drop_path_rate = model_args.drop_path_rate
    if config.llm_config.model_type == 'internlm2':
        config.llm_config.attn_implementation = 'flash_attention_2'  # for InternLM
        logger.info('Using flash_attention_2 for InternLM')
    else:
        config.llm_config._attn_implementation = 'flash_attention_2'  # for LLaMA
        logger.info('Using flash_attention_2 for LLaMA')
    config.template = data_args.conv_style
    config.select_layer = model_args.vision_select_layer
    config.dynamic_image_size = data_args.dynamic_image_size
    config.use_thumbnail = data_args.use_thumbnail
    config.ps_version = model_args.ps_version
    config.min_dynamic_patch = data_args.min_dynamic_patch
    config.max_dynamic_patch = data_args.max_dynamic_patch
    model = InternVLChatModel.from_pretrained(
        model_args.model_name_or_path,
        torch_dtype=torch.bfloat16,
        config=config,
    )

    model.img_context_token_id = img_context_token_id

    assert model.config.downsample_ratio == data_args.down_sample_ratio
    assert model_args.mlp_path is None

    patch_size = model.config.vision_config.patch_size
    logger.info(f'model.config.force_image_size: {model.config.force_image_size}')
    logger.info(f'data_args.force_image_size: {data_args.force_image_size}')
    logger.info(f'model.config.vision_config.image_size: {model.config.vision_config.image_size}')
    if model.config.vision_config.image_size != data_args.force_image_size:
        logger.info(f'Resizing position embedding from '
                    f'{model.config.vision_config.image_size} '
                    f'to {data_args.force_image_size}...')
        model.vision_model.resize_pos_embeddings(old_size=model.config.vision_config.image_size,
                                                 new_size=data_args.force_image_size,
                                                 patch_size=patch_size)
        model.config.vision_config.image_size = data_args.force_image_size
    model.config.force_image_size = data_args.force_image_size
    model.num_image_token = int((data_args.force_image_size // patch_size) ** 2 * (data_args.down_sample_ratio ** 2))

    if num_new_tokens > 0:
        model.language_model.resize_token_embeddings(len(tokenizer))
        output_embeddings = model.language_model.get_output_embeddings().weight.data
        output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)
        output_embeddings[-num_new_tokens:] = output_embeddings_avg

        model.config.llm_config.vocab_size = len(tokenizer)
        model.language_model.config.vocab_size = len(tokenizer)

    train_dataset = build_datasets(
        data_args, tokenizer, tcs_loader, model, group_by_length=training_args.group_by_length,
        dynamic_image_size=data_args.dynamic_image_size, use_thumbnail=data_args.use_thumbnail,
        min_dynamic_patch=data_args.min_dynamic_patch, max_dynamic_patch=data_args.max_dynamic_patch,
        normalize_type=data_args.normalize_type, min_num_frame=data_args.min_num_frame,
        max_num_frame=data_args.max_num_frame)

    def _freeze_params(module):
        for param in module.parameters():
            param.requires_grad = False

    model.eval()
    _freeze_params(model.vision_model)
    _freeze_params(model.language_model)

    # set seed for torch dataloaders
    set_seed(training_args.seed)

    training_args.precompute_ref_log_probs = True
    trainer = MultimodalDPOTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=None,
        tokenizer=tokenizer,
        data_collator=dpo_concat_pad_data_collator,
    )

    # Training
    print(os.environ.get('PYTORCH_CUDA_ALLOC_CONF', None))
    results_file = os.path.join(
        training_args.output_dir,
        f'{os.path.basename(model_args.model_name_or_path)}_{data_args.max_seq_length}.pt',
    )
    trainer.pre_compute_logps(results_file)


if __name__ == '__main__':
    main()
