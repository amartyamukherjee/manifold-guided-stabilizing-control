import os
import cv2
import copy
import math
import argparse
from matplotlib import pyplot as plt
import numpy as np
from time import time
from tqdm import tqdm
from easydict import EasyDict
from PIL import Image

import torch
import torch.distributed as dist
from torch import nn
import torch.nn.functional as F
from torch.optim import Adam
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP

from data import get_metadata, get_dataset, fix_legacy_dict
import unets

unsqueeze3x = lambda x: x[..., None, None, None]

x = y = np.linspace(-1,1,64)
xx,yy = np.meshgrid(x,y)
xx_t,yy_t = torch.Tensor(xx),torch.Tensor(yy)
coords = torch.stack((xx_t,yy_t)).view(2,-1)

class Pendulum(nn.Module):
    def __init__(self):
        super().__init__()
        x = y = np.linspace(-1,1,64)
        xx,yy = np.meshgrid(x,y)
        self.xx = torch.Tensor(xx).to("cuda:0")
        self.yy = torch.Tensor(yy).to("cuda:0")
        self.m = 0.15
        self.g = 9.81
        self.l = 0.5
        # self.l = self.g

        self.register_parameter("phi1", nn.Parameter(torch.randn(())))
        self.register_parameter("phi2", nn.Parameter(torch.randn(())))
        # self.register_parameter("phi3", nn.Parameter(torch.randn(())))
        self.coeffs = {name: param for name, param in self.named_parameters()}
    
    def forward(self,V):
        control = 5 * F.tanh(self.coeffs["phi1"]*self.xx) + 5 * F.tanh(self.coeffs["phi2"]*self.yy)
        f1 = self.yy
        f2 = self.g*torch.sin(self.xx)/self.l + (control - 0.1*self.yy) / (self.m*self.l*self.l)
        # f3 = torch.zeros_like(self.xx)
        return torch.stack((f1,f2,V.to("cuda:0")))
    
class NoisyPendulum(nn.Module):
    def __init__(self):
        super().__init__()
        x = y = np.linspace(-1,1,64)
        xx,yy = np.meshgrid(x,y)
        self.xx = torch.Tensor(xx).to("cuda:0")
        self.yy = torch.Tensor(yy).to("cuda:0")
        self.m = 0.15
        self.g = 9.81
        self.l = 0.5
        # self.l = self.g

        self.register_parameter("phi1", nn.Parameter(torch.randn(())))
        self.register_parameter("phi2", nn.Parameter(torch.randn(())))
        # self.register_parameter("phi3", nn.Parameter(torch.randn(())))
        self.coeffs = {name: param for name, param in self.named_parameters()}
    
    def forward(self,V):
        m = self.m + np.random.uniform(low=-0.05, high=0.05)
        g = self.g + np.random.uniform(low=-0.05, high=0.05)
        l = self.l + np.random.uniform(low=-0.05, high=0.05)
        control = 5 * F.tanh(self.coeffs["phi1"]*self.xx) + 5 * F.tanh(self.coeffs["phi2"]*self.yy)
        noise = torch.rand(())*0.1-0.05
        control *= 1+noise
        f1 = self.yy
        f2 = g*torch.sin(self.xx)/l + (control - 0.1*self.yy) / (m*l*l)
        # f3 = torch.zeros_like(self.xx)
        return torch.stack((f1,f2,V.to("cuda:0")))
    
class Duffing(nn.Module):
    def __init__(self):
        super().__init__()
        x = y = np.linspace(-1,1,64)
        xx,yy = np.meshgrid(x,y)
        self.xx = torch.Tensor(xx).to("cuda:0")
        self.yy = torch.Tensor(yy).to("cuda:0")

        self.register_parameter("phi1", nn.Parameter(torch.randn(())))
        self.register_parameter("phi2", nn.Parameter(torch.randn(())))
        # self.register_parameter("phi3", nn.Parameter(torch.randn(())))
        self.coeffs = {name: param for name, param in self.named_parameters()}
    
    def forward(self,V):
        # control = 20 * F.tanh(self.coeffs["phi1"]*self.xx + self.coeffs["phi2"]*self.yy)
        control = 20 * F.tanh(self.coeffs["phi1"]*self.xx) + 20 * F.tanh(self.coeffs["phi2"]*self.yy)
        f1 = self.yy
        f2 = -0.5*self.yy - self.xx * (4*self.xx*self.xx - 1) + 0.5 * control
        # f3 = torch.zeros_like(self.xx)
        return torch.stack((f1,f2,V.to("cuda:0")))

class VanDerPol(nn.Module):
    def __init__(self):
        super().__init__()
        x = y = np.linspace(-1,1,64)
        xx,yy = np.meshgrid(x,y)
        self.xx = torch.Tensor(xx).to("cuda:0")
        self.yy = torch.Tensor(yy).to("cuda:0")

        self.register_parameter("phi1", nn.Parameter(torch.randn(())))
        self.register_parameter("phi2", nn.Parameter(torch.randn(())))
        # self.register_parameter("phi3", nn.Parameter(torch.randn(())))
        self.coeffs = {name: param for name, param in self.named_parameters()}
    
    def forward(self,V):
        # control = 20 * F.tanh(self.coeffs["phi1"]*self.xx + self.coeffs["phi2"]*self.yy)
        control = 20 * F.tanh(self.coeffs["phi1"]*self.xx) + 20 * F.tanh(self.coeffs["phi2"]*self.yy)
        f1 = 2*self.yy
        f2 = -0.8*self.xx + 2*self.yy - 10*self.xx*self.xx*self.yy + control
        # f3 = torch.zeros_like(self.xx)
        return torch.stack((f1,f2,V.to("cuda:0")))

system_dict = {
    "noisy_pendulum": NoisyPendulum,
    "pendulum": Pendulum,
    "duffing": Duffing,
    "van_der_pol": VanDerPol
}

def plot_fn_lyap(img,fig_title,v=None):
    if v is None:
        fig, ax = plt.subplots(1,3,figsize=(12, 4))
    else:
        fig, ax = plt.subplots(1,4,figsize=(16, 4))
        v = v / v.abs().max()
        v = v.detach().cpu().numpy()

    img = img.detach().cpu().numpy()
    f1 = img[0]
    f2 = img[1]
    V = img[2]

    ax[0].imshow(f1)
    ax[0].set(title="f1")
    ax[1].imshow(f2)
    ax[1].set(title="f2")
    ax[2].imshow(V)
    ax[2].set(title="DDIM V")
    if v is not None:
        ax[3].imshow(v)
        ax[3].set(title="True V")
        cbar3 = fig.colorbar(ax[3].imshow(v), ax=ax[3])


    cbar0 = fig.colorbar(ax[0].imshow(f1), ax=ax[0])
    cbar1 = fig.colorbar(ax[1].imshow(f2), ax=ax[1])
    cbar2 = fig.colorbar(ax[2].imshow(V), ax=ax[2])

    plt.savefig(fig_title)

def plot_fn_step(final,pred_x0,t):
    f1 = final[0,0,:,:].detach().cpu().numpy()
    f2 = final[0,1,:,:].detach().cpu().numpy()

    f0_1 = pred_x0[0,0,:,:].detach().cpu().numpy()
    f0_2 = pred_x0[0,1,:,:].detach().cpu().numpy()

    V = pred_x0[0,2,:,:].detach().cpu().numpy()

    img_dict = {
        "f1":f1,
        "f2":f2,
        "f0_1":f0_1,
        "f0_2":f0_2,
        "V":V,
    }

    for k,v in img_dict.items():
        plt.imshow(v)
        plt.savefig("paper_figs/"+k+"_"+str(t)+".png")

class GuassianDiffusion:
    """Gaussian diffusion process with 1) Cosine schedule for beta values (https://arxiv.org/abs/2102.09672)
    2) L_simple training objective from https://arxiv.org/abs/2006.11239.
    """

    def __init__(self, timesteps=1000, device="cuda:0"):
        self.timesteps = timesteps
        self.device = device
        self.alpha_bar_scheduler = (
            lambda t: math.cos((t / self.timesteps + 0.008) / 1.008 * math.pi / 2) ** 2
        )
        self.scalars = self.get_all_scalars(
            self.alpha_bar_scheduler, self.timesteps, self.device
        )

        self.clamp_x0 = lambda x: x.clamp(-1, 1)
        self.get_x0_from_xt_eps = lambda xt, eps, t, scalars: (
            self.clamp_x0(
                1
                / unsqueeze3x(scalars.alpha_bar[t].sqrt())
                * (xt - unsqueeze3x((1 - scalars.alpha_bar[t]).sqrt()) * eps)
            )
        )
        self.get_pred_mean_from_x0_xt = (
            lambda xt, x0, t, scalars: unsqueeze3x(
                (scalars.alpha_bar[t].sqrt() * scalars.beta[t])
                / ((1 - scalars.alpha_bar[t]) * scalars.alpha[t].sqrt())
            )
            * x0
            + unsqueeze3x(
                (scalars.alpha[t] - scalars.alpha_bar[t])
                / ((1 - scalars.alpha_bar[t]) * scalars.alpha[t].sqrt())
            )
            * xt
        )

    def get_all_scalars(self, alpha_bar_scheduler, timesteps, device, betas=None):
        """
        Using alpha_bar_scheduler, get values of all scalars, such as beta, beta_hat, alpha, alpha_hat, etc.
        """
        all_scalars = {}
        if betas is None:
            all_scalars["beta"] = torch.from_numpy(
                np.array(
                    [
                        min(
                            1 - alpha_bar_scheduler(t + 1) / alpha_bar_scheduler(t),
                            0.999,
                        )
                        for t in range(timesteps)
                    ]
                )
            ).to(
                device
            )  # hardcoding beta_max to 0.999
        else:
            all_scalars["beta"] = betas
        all_scalars["beta_log"] = torch.log(all_scalars["beta"])
        all_scalars["alpha"] = 1 - all_scalars["beta"]
        all_scalars["alpha_bar"] = torch.cumprod(all_scalars["alpha"], dim=0)
        all_scalars["beta_tilde"] = (
            all_scalars["beta"][1:]
            * (1 - all_scalars["alpha_bar"][:-1])
            / (1 - all_scalars["alpha_bar"][1:])
        )
        all_scalars["beta_tilde"] = torch.cat(
            [all_scalars["beta_tilde"][0:1], all_scalars["beta_tilde"]]
        )
        all_scalars["beta_tilde_log"] = torch.log(all_scalars["beta_tilde"])
        return EasyDict(dict([(k, v.float()) for (k, v) in all_scalars.items()]))

    def sample_from_forward_process(self, x0, t):
        """Single step of the forward process, where we add noise in the image.
        Note that we will use this paritcular realization of noise vector (eps) in training.
        """
        eps = torch.randn_like(x0)
        xt = (
            unsqueeze3x(self.scalars.alpha_bar[t].sqrt()) * x0
            + unsqueeze3x((1 - self.scalars.alpha_bar[t]).sqrt()) * eps
        )
        return xt.float(), eps

    def sample_from_reverse_process(
        self, model, system, timesteps=None, model_kwargs={}, ddim=False
    ):
        """Sampling images by iterating over all timesteps.

        model: diffusion model
        xT: Starting noise vector.
        timesteps: Number of sampling steps (can be smaller the default,
            i.e., timesteps in the diffusion process).
        model_kwargs: Additional kwargs for model (using it to feed class label for conditioning)
        ddim: Use ddim sampling (https://arxiv.org/abs/2010.02502). With very small number of
            sampling steps, use ddim sampling for better image quality.

        Return: An image tensor with identical shape as XT.
        """
        model.eval()
        # final = xT

        p = system().to("cuda:0")
        opt = Adam(p.parameters(), lr=0.1)
        vT = torch.randn((64,64))

        final = p(vT).unsqueeze(0)
        norm = final[0,:2,:,:].abs().max().detach()
        final = final / norm

        # sub-sampling timesteps for faster sampling
        timesteps = timesteps or self.timesteps
        new_timesteps = np.linspace(
            0, self.timesteps - 1, num=timesteps, endpoint=True, dtype=int
        )
        alpha_bar = self.scalars["alpha_bar"][new_timesteps]
        new_betas = 1 - (
            alpha_bar / torch.nn.functional.pad(alpha_bar, [1, 0], value=1.0)[:-1]
        )
        scalars = self.get_all_scalars(
            self.alpha_bar_scheduler, timesteps, self.device, new_betas
        )

        for i, t in zip(np.arange(timesteps)[::-1], new_timesteps[::-1]):
            # print(t)
            # with torch.no_grad():
            current_t = torch.tensor([t] * len(final), device=final.device)
            current_sub_t = torch.tensor([i] * len(final), device=final.device)
            pred_epsilon = model(final, current_t, **model_kwargs).detach()
            # using xt+x0 to derive mu_t, instead of using xt+eps (former is more stable)
            pred_x0 = self.get_x0_from_xt_eps(
                final, pred_epsilon, current_sub_t, scalars
            )
            pred_x0_f = pred_x0[:,0:2,:,:]
            # print(final[0,1,:,:])
            # print(pred_x0_f[0,1,:,:])
            pred_x0_V = pred_x0[0,2,:,:]

            loss = F.mse_loss(final[:,0:2,:,:],pred_x0_f)
            opt.zero_grad()
            loss.backward(retain_graph=True)
            opt.step()
            print("LOSS: ", loss.item(), "PARAMS: ", {"phi1": p.coeffs["phi1"].item(), "phi2": p.coeffs["phi2"].item()})

            # if t in [970,942,898]:
            #     plot_fn_step(final,pred_x0,t)

            final = p(pred_x0_V).unsqueeze(0)
            norm = final[0,:2,:,:].abs().max().detach()
            final[0,:2,:,:] = final[0,:2,:,:] / norm

        final = p(pred_x0_V).detach()
        plot_fn_lyap(final, "lyap_results.png")
        # plot_fn_lyap(final, "lyap_results2.png", p.true_lyap_fn().detach())
        print("img saved in lyap_results.png")
        return final


class loss_logger:
    def __init__(self, max_steps):
        self.max_steps = max_steps
        self.loss = []
        self.start_time = time()
        self.ema_loss = None
        self.ema_w = 0.9

    def log(self, v, display=False):
        self.loss.append(v)
        if self.ema_loss is None:
            self.ema_loss = v
        else:
            self.ema_loss = self.ema_w * self.ema_loss + (1 - self.ema_w) * v

        if display:
            print(
                f"Steps: {len(self.loss)}/{self.max_steps} \t loss (ema): {self.ema_loss:.3f} "
                + f"\t Time elapsed: {(time() - self.start_time)/3600:.3f} hr"
            )


def train_one_epoch(
    model,
    dataloader,
    diffusion,
    optimizer,
    logger,
    lrs,
    args,
):
    model.train()
    for step, images in enumerate(dataloader):
        # must use [-1, 1] pixel range for images
        if args.dataset in ["poisson","darcy","lyapunov"]:
            images, labels = (
                images.to(args.device),
                labels.to(args.device) if args.class_cond else None,
            )
            if args.dataset == "darcy":
                images = images / 2.06
            if args.dataset == "lyapunov":
                images = images / images.max().abs()
        else:
            images, labels = images
            assert (images.max().item() <= 1) and (0 <= images.min().item())
            images, labels = (
                2 * images.to(args.device) - 1,
                labels.to(args.device) if args.class_cond else None,
            )
        t = torch.randint(diffusion.timesteps, (len(images),), dtype=torch.int64).to(
            args.device
        )
        xt, eps = diffusion.sample_from_forward_process(images, t)
        pred_eps = model(xt, t, y=labels)

        loss = ((pred_eps - eps) ** 2).mean()
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        if lrs is not None:
            lrs.step()

        # update ema_dict
        if args.local_rank == 0:
            new_dict = model.state_dict()
            for (k, v) in args.ema_dict.items():
                args.ema_dict[k] = (
                    args.ema_w * args.ema_dict[k] + (1 - args.ema_w) * new_dict[k]
                )
            logger.log(loss.item(), display=not step % 100)

def sample_N_images(
    N,
    model,
    diffusion,
    system="noisy_pendulum",
    sampling_steps=250,
    batch_size=64,
    num_channels=3,
    image_size=32,
    num_classes=None,
    args=None,
):
    """use this function to sample any number of images from a given
        diffusion model and diffusion process.

    Args:
        N : Number of images
        model : Diffusion model
        diffusion : Diffusion process
        xT : Starting instantiation of noise vector.
        sampling_steps : Number of sampling steps.
        batch_size : Batch-size for sampling.
        num_channels : Number of channels in the image.
        image_size : Image size (assuming square images).
        num_classes : Number of classes in the dataset (needed for class-conditioned models)
        args : All args from the argparser.

    Returns: Numpy array with N images and corresponding labels.
    """
    N = 1
    samples, labels, num_samples = [], [], 0
    num_processes, group = dist.get_world_size(), dist.group.WORLD
    with tqdm(total=math.ceil(N / (args.batch_size * num_processes))) as pbar:
        while num_samples < N:
            assert system in system_dict.keys()
            system = system_dict[system]
            gen_images = diffusion.sample_from_reverse_process(
                model, system, sampling_steps, {"y": y}, args.ddim
            )
            samples_list = [torch.zeros_like(gen_images) for _ in range(num_processes)]
            if args.class_cond:
                labels_list = [torch.zeros_like(y) for _ in range(num_processes)]
                dist.all_gather(labels_list, y, group)
                labels.append(torch.cat(labels_list).detach().cpu().numpy())

            dist.all_gather(samples_list, gen_images, group)
            if args.dataset in ["poisson","darcy","lyapunov"]:
                samples.append(torch.cat(samples_list).detach().cpu())
            else:
                samples.append(torch.cat(samples_list).detach().cpu().numpy())
            num_samples += num_processes
            pbar.update(1)
    if args.dataset in ["poisson","darcy","lyapunov"]:
        samples = torch.cat(samples)
    else:
        samples = np.concatenate(samples).transpose(0, 2, 3, 1)[:N]
        samples = (127.5 * (samples + 1)).astype(np.uint8)
    return (samples, np.concatenate(labels) if args.class_cond else None)


def main():
    parser = argparse.ArgumentParser("Minimal implementation of diffusion models")
    # diffusion model
    parser.add_argument("--arch", default="UNet", type=str, help="Neural network architecture")
    parser.add_argument(
        "--class-cond",
        action="store_true",
        default=False,
        help="train class-conditioned diffusion model",
    )
    parser.add_argument(
        "--diffusion-steps",
        type=int,
        default=1000,
        help="Number of timesteps in diffusion process",
    )
    parser.add_argument(
        "--sampling-steps",
        type=int,
        default=250,
        help="Number of timesteps in diffusion process",
    )
    parser.add_argument(
        "--ddim",
        action="store_true",
        default=True,
        help="Sampling using DDIM update step",
    )
    # dataset
    parser.add_argument("--dataset", type=str, default="lyapunov")
    parser.add_argument("--data-dir", type=str, default="./dataset/")
    parser.add_argument("--system", type=str, default="noisy_pendulum")
    # optimizer
    parser.add_argument(
        "--batch-size", type=int, default=128, help="batch-size per gpu"
    )
    parser.add_argument("--lr", type=float, default=0.0001)
    parser.add_argument("--epochs", type=int, default=500)
    parser.add_argument("--ema_w", type=float, default=0.9995)
    # sampling/finetuning
    parser.add_argument("--pretrained-ckpt", 
                        default="trained_models/UNet_lyapunov-epoch_500-timesteps_1000-class_condn_False.pt", 
                        type=str, 
                        help="Pretrained model ckpt")
    parser.add_argument("--delete-keys", nargs="+", help="Pretrained model ckpt")
    parser.add_argument(
        "--sampling-only",
        action="store_true",
        default=True,
        help="No training, just sample images (will save them in --save-dir)",
    )
    parser.add_argument(
        "--num-sampled-images",
        type=int,
        default=50000,
        help="Number of images required to sample from the model",
    )

    # misc
    parser.add_argument("--save-dir", type=str, default="./trained_models/")
    parser.add_argument("--local-rank", default=2, type=int)
    parser.add_argument("--seed", default=112233, type=int)

    # setup
    args = parser.parse_args()
    metadata = get_metadata(args.dataset)
    os.makedirs(args.save_dir, exist_ok=True)
    torch.backends.cudnn.benchmark = True
    args.device = "cuda:{}".format(args.local_rank)
    torch.cuda.set_device(args.device)
    torch.manual_seed(args.seed + args.local_rank)
    np.random.seed(args.seed + args.local_rank)
    if args.local_rank == 0:
        print(args)

    # Creat model and diffusion process
    model = unets.__dict__[args.arch](
        image_size=metadata.image_size,
        in_channels=metadata.num_channels,
        out_channels=metadata.num_channels,
        num_classes=metadata.num_classes if args.class_cond else None,
    ).to(args.device)
    if args.local_rank == 0:
        print(
            "We are assuming that model input/ouput pixel range is [-1, 1]. Please adhere to it."
        )
    diffusion = GuassianDiffusion(args.diffusion_steps, args.device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)

    # load pre-trained model
    if args.pretrained_ckpt:
        print(f"Loading pretrained model from {args.pretrained_ckpt}")
        d = fix_legacy_dict(torch.load(args.pretrained_ckpt, map_location=args.device))
        dm = model.state_dict()
        if args.delete_keys:
            for k in args.delete_keys:
                print(
                    f"Deleting key {k} becuase its shape in ckpt ({d[k].shape}) doesn't match "
                    + f"with shape in model ({dm[k].shape})"
                )
                del d[k]
        model.load_state_dict(d, strict=False)
        print(
            f"Mismatched keys in ckpt and model: ",
            set(d.keys()) ^ set(dm.keys()),
        )
        print(f"Loaded pretrained model from {args.pretrained_ckpt}")

    # distributed training
    ngpus = torch.cuda.device_count()
    if ngpus > 1:
        if args.local_rank == 0:
            print(f"Using distributed training on {ngpus} gpus.")
        args.batch_size = args.batch_size // ngpus
        torch.distributed.init_process_group(backend="nccl", init_method="env://")
        model = DDP(model, device_ids=[args.local_rank], output_device=args.local_rank)

    # sampling
    if args.sampling_only:
        print(f"Sampling only")
        sampled_images, labels = sample_N_images(
            args.num_sampled_images,
            model,
            diffusion,
            args.system,
            args.sampling_steps,
            args.batch_size,
            metadata.num_channels,
            metadata.image_size,
            metadata.num_classes,
            args,
        )
        np.savez(
            os.path.join(
                args.save_dir,
                f"{args.arch}_{args.dataset}-{args.sampling_steps}-sampling_steps-{len(sampled_images)}_images-class_condn_{args.class_cond}.npz",
            ),
            sampled_images,
            labels,
        )
        return

    # Load dataset
    train_set = get_dataset(args.dataset, args.data_dir, metadata)
    sampler = DistributedSampler(train_set) if ngpus > 1 else None
    train_loader = DataLoader(
        train_set,
        batch_size=args.batch_size,
        shuffle=sampler is None,
        sampler=sampler,
        num_workers=4,
        pin_memory=True,
    )
    if args.local_rank == 0:
        print(
            f"Training dataset loaded: Number of batches: {len(train_loader)}, Number of images: {len(train_set)}"
        )
    logger = loss_logger(len(train_loader) * args.epochs)

    # ema model
    args.ema_dict = copy.deepcopy(model.state_dict())

    # lets start training the model
    for epoch in range(args.epochs):
        if sampler is not None:
            sampler.set_epoch(epoch)
        train_one_epoch(model, train_loader, diffusion, optimizer, logger, None, args)
        if args.local_rank == 0:
            torch.save(
                model.state_dict(),
                os.path.join(
                    args.save_dir,
                    f"{args.arch}_{args.dataset}-epoch_{args.epochs}-timesteps_{args.diffusion_steps}-class_condn_{args.class_cond}.pt",
                ),
            )
            torch.save(
                args.ema_dict,
                os.path.join(
                    args.save_dir,
                    f"{args.arch}_{args.dataset}-epoch_{args.epochs}-timesteps_{args.diffusion_steps}-class_condn_{args.class_cond}_ema_{args.ema_w}.pt",
                ),
            )
        if not epoch % 1:
            sampled_images, _ = sample_N_images(
                64,
                model,
                diffusion,
                None,
                args.sampling_steps,
                args.batch_size,
                metadata.num_channels,
                metadata.image_size,
                metadata.num_classes,
                args,
            )
            if args.local_rank == 0:
                if args.dataset in ["poisson","darcy","lyapunov"]:
                    torch.save(sampled_images,
                               os.path.join(
                                    args.save_dir,
                                    f"{args.arch}_{args.dataset}-{args.diffusion_steps}_steps-{args.sampling_steps}-sampling_steps-class_condn_{args.class_cond}.pt",
                                ))
                else:
                    cv2.imwrite(
                        os.path.join(
                            args.save_dir,
                            f"{args.arch}_{args.dataset}-{args.diffusion_steps}_steps-{args.sampling_steps}-sampling_steps-class_condn_{args.class_cond}.png",
                        ),
                        np.concatenate(sampled_images, axis=1)[:, :, ::-1],
                    )

if __name__ == "__main__":
    main()
