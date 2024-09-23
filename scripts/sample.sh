#!/bin/bash

# First download pretrained diffusion models from https://drive.google.com/drive/folders/1BMTpNF-FSsGrWGZomcM4OS36CootbLRj?usp=sharing
# You can also find 50,000 pre-sampled synthetic images for each dataset at 
# https://drive.google.com/drive/folders/1KRWie7honV_mwPlmTgH8vrU0izQXm4UT?usp=sharing

CUDA_VISIBLE_DEVICES=0 python restoration_control.py \
    --arch UNet --dataset lyapunov --system noisy_pendulum --sampling-steps 250 \
    --pretrained-ckpt ./trained_models/path_to_saved_model.pt --save-dir ./sampled_images/

CUDA_VISIBLE_DEVICES=0 python restoration_control.py \
    --arch UNet --dataset lyapunov --system pendulum --sampling-steps 250 \
    --pretrained-ckpt ./trained_models/path_to_saved_model.pt --save-dir ./sampled_images/

CUDA_VISIBLE_DEVICES=0 python restoration_control.py \
    --arch UNet --dataset lyapunov --system duffing --sampling-steps 250 \
    --pretrained-ckpt ./trained_models/path_to_saved_model.pt --save-dir ./sampled_images/

CUDA_VISIBLE_DEVICES=0 python restoration_control.py \
    --arch UNet --dataset lyapunov --system van_der_pol --sampling-steps 250 \
    --pretrained-ckpt ./trained_models/path_to_saved_model.pt --save-dir ./sampled_images/
