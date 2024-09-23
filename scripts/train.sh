#!/bin/bash

# Change data-dir to refer to the path of training dataset on your machine
# Following datasets needs to be manually downloaded before training: melanoma, afhq, celeba, cars, flowers, gtsrb.
CUDA_VISIBLE_DEVICES=1,2,3,4 python -m torch.distributed.launch --nproc_per_node=4 main.py \
    --arch UNet --dataset lyapunov --epochs 500
