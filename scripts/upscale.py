import sys
import subprocess
import pkg_resources

required = {"torch", "torchvision", "numpy", "opencv-python", "spandrel", "Pillow"}
installed = {pkg.key for pkg in pkg_resources.working_set}
missing = required - installed

if missing:
    python = sys.executable
    subprocess.check_call([python, "-m", "pip", "install", *missing], stdout=subprocess.DEVNULL)

import torch
import torchvision
import cv2
import numpy as np
import spandrel
from PIL import Image
from RRDB import RRDBNet
from SPSR import SPSRNet
from SRVGG import SRVGGNetCompact
import argparse

device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"

def load_image(image, downscale):
    img = Image.open(image).convert("RGB")
    if downscale:
        img.thumbnail((int(downscale), int(downscale)))
    transform = torchvision.transforms.Compose([torchvision.transforms.ToTensor()])
    return transform(img).to(device)

def upscale_other(input_path, output_path, model_path, downscale):
    model = spandrel.ModelLoader().load_from_file(model_path).eval().to(device)
    image = load_image(input_path, downscale)
    image = image.unsqueeze(0)
    output = model(image)
    output = output.squeeze(0)
    torchvision.utils.save_image(output, output_path)

def process(img, model, fp16=False):
    if img.shape[2] == 3:
        img = img[:, :, [2, 1, 0]]
    elif img.shape[2] == 4:
        img = img[:, :, [2, 1, 0, 3]]
    img = torch.from_numpy(np.transpose(img, (2, 0, 1))).float()
    if fp16:
        img = img.half()
    img = img.unsqueeze(0)
    img = img.to(device)
    output = model(img).data.squeeze(0).float().cpu().clamp_(0, 1).numpy()
    if output.shape[0] == 3:
        output = output[[2, 1, 0], :, :]
    elif output.shape[0] == 4:
        output = output[[2, 1, 0, 3], :, :]
    output = np.transpose(output, (1, 2, 0))
    return output

def image_resize(image, size, inter=cv2.INTER_AREA):
    dim = None
    (h, w) = image.shape[:2]
    if w < h:
        r = size / float(h)
        dim = (int(w * r), size)
    else:
        r = size / float(w)
        dim = (size, int(h * r))

    resized = cv2.resize(image, dim, interpolation=inter)
    return resized

def upscale(input_path, output_path, model_path, downscale):
    img = cv2.imdecode(np.fromfile(input_path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    state_dict = torch.load(model_path)
    model = None
    last_in_nc = None
    last_out_nc = None
    if ("params" in state_dict.keys() and "body.0.weight" in state_dict["params"].keys()):
        model = SRVGGNetCompact(state_dict)
        last_in_nc = model.num_in_ch
        last_out_nc = model.num_out_ch
    elif "f_HR_conv1.0.weight" in state_dict:
        model = SPSRNet(state_dict)
        last_in_nc = model.in_nc
        last_out_nc = model.out_nc
    elif "model.1.sub.0.RDB1.conv1.0.weight" in state_dict:
        model = RRDBNet(state_dict)
        last_in_nc = model.in_nc
        last_out_nc = model.out_nc
    else:
        return upscale_other(input_path, output_path, model_path, downscale)
    model.eval()
    for k, v in model.named_parameters():
        v.requires_grad = False
    model = model.to(device)

    if downscale:
        img = image_resize(img, int(downscale))

    img = img * 1.0 / np.iinfo(img.dtype).max
    output = None
    if (img.ndim == 3 and img.shape[2] == 4 and last_in_nc == 3 and last_out_nc == 3):
        img1 = np.copy(img[:, :, :3])
        img2 = np.copy(img[:, :, :3])
        for c in range(3):
            img1[:, :, c] *= img[:, :, 3]
            img2[:, :, c] = (img2[:, :, c] - 1) * img[:, :, 3] + 1
        output1 = process(img1, model)
        output2 = process(img2, model)
        alpha = 1 - np.mean(output2 - output1, axis=2)
        output = np.dstack((output1, alpha))
        output = np.clip(output, 0, 1)
    else:
        if img.ndim == 2:
            img = np.tile(np.expand_dims(img, axis=2), (1, 1, min(last_in_nc, 3)))
        if img.shape[2] > last_in_nc:
            img = img[:, :, : last_in_nc]
        elif img.shape[2] == 3 and last_in_nc == 4:
            img = np.dstack((img, np.full(img.shape[:-1], 1.0)))
        output = process(img, model)
    output = (output * 255.0).round()
    cv2.imwrite(output_path, output)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="Upscaler")
    parser.add_argument("-i", "--input")
    parser.add_argument("-o", "--output")
    parser.add_argument("-m", "--model")
    parser.add_argument("-d", "--downscale")
    args = parser.parse_args()
    upscale(args.input, args.output, args.model, args.downscale)