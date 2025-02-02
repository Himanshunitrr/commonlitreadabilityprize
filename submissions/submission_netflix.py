import getpass
import gc
from os import path
from pathlib import Path

import pandas as pd
import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn as nn
from sklearn.linear_model import LinearRegression, RidgeCV, LassoCV
from sklearn.metrics import mean_squared_error
from sklearn.model_selection import StratifiedKFold
from torch.utils.data import DataLoader, Dataset

KERNEL = False if getpass.getuser() == "anjum" else True

if not KERNEL:
    INPUT_PATH = Path("/mnt/storage_dimm2/kaggle_data/commonlitreadabilityprize")
    OUTPUT_PATH = Path("/mnt/storage/kaggle_output/commonlitreadabilityprize")
    MODEL_CACHE = Path("/mnt/storage/model_cache/torch")
else:
    INPUT_PATH = Path("../input/commonlitreadabilityprize")
    MODEL_CACHE = None

    # Install packages
    import subprocess

    whls = [
        "../input/textstat/Pyphen-0.10.0-py3-none-any.whl",
        "../input/textstat/textstat-0.7.0-py3-none-any.whl",
        "../input/transformers461/huggingface_hub-0.0.10-py3-none-any.whl",
        "../input/transformers461/transformers-4.6.1-py3-none-any.whl",
    ]

    for w in whls:
        print("Installing", w)
        subprocess.call(["pip", "install", w, "--no-deps", "--upgrade"])

import textstat
from transformers import (
    AutoConfig,
    AutoModel,
    AdamW,
)
from transformers.models.auto.tokenization_auto import AutoTokenizer

import transformers

print("Transformers version:", transformers.__version__)


# models.py
class AttentionBlock(nn.Module):
    def __init__(self, in_features, middle_features, out_features):
        super().__init__()
        self.in_features = in_features
        self.middle_features = middle_features
        self.out_features = out_features
        self.W = nn.Linear(in_features, middle_features)
        self.V = nn.Linear(middle_features, out_features)

    def forward(self, features):
        att = torch.tanh(self.W(features))
        score = self.V(att)
        attention_weights = torch.softmax(score, dim=1)
        context_vector = attention_weights * features
        context_vector = torch.sum(context_vector, dim=1)
        return context_vector


class CommonLitModel(pl.LightningModule):
    def __init__(
        self,
        model_name: str = "roberta-base",
        lr: float = 0.001,
        weight_decay: float = 0,
        pretrained: bool = False,
        betas: tuple = (0.9, 0.999),
        eps: float = 1e-6,
        kl_loss: bool = False,
        warmup: int = 100,
        hf_config=None,
        pooled: bool = False,
        use_hidden: bool = False,
        **kwargs,
    ):
        super().__init__()
        self.save_hyperparameters()

        if hf_config is None:
            if pretrained:
                model_path = OUTPUT_PATH / "pretraining" / model_name
                print("Using pretrained from", model_path)
                self.config = AutoConfig.from_pretrained(model_name)
                self.transformer = AutoModel.from_pretrained(
                    model_path, output_hidden_states=True
                )
            else:
                self.config = AutoConfig.from_pretrained(
                    model_name,
                    cache_dir=MODEL_CACHE / model_name,
                )
                self.transformer = AutoModel.from_pretrained(
                    model_name,
                    cache_dir=MODEL_CACHE / model_name,
                    output_hidden_states=True,
                )
        else:
            self.config = hf_config
            self.config.output_hidden_states = True
            self.transformer = AutoModel.from_config(hf_config)

        # self.layer_norm = nn.LayerNorm(self.config.hidden_size)
        # Multi sample Dropout
        # self.dropouts = nn.ModuleList([nn.Dropout(0.5) for _ in range(5)])
        # self.dropouts = nn.ModuleList([nn.Dropout(0.3)])
        # self.regressor = nn.Linear(self.config.hidden_size, 2)
        # self._init_weights(self.layer_norm)
        # self._init_weights(self.regressor)

        if use_hidden:
            n_hidden = self.config.hidden_size * 2
        else:
            n_hidden = self.config.hidden_size

        self.seq_attn_head = nn.Sequential(
            nn.LayerNorm(n_hidden),
            # nn.Dropout(0.1),
            AttentionBlock(n_hidden, n_hidden, 1),
            # nn.Dropout(0.1),
            # nn.Linear(self.config.hidden_size, 2 if kl_loss else 1),
        )

        self.regressor = nn.Linear(n_hidden + 2, 2 if kl_loss else 1)

        self.loss_fn = nn.MSELoss()

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)

    def forward(self, features, **kwargs):
        # out = self.transformer(**kwargs)["logits"]

        model_out = self.transformer(**kwargs)  # 0=seq_output, 1=pooler_output
        # x = self.layer_norm(x)
        # for i, dropout in enumerate(self.dropouts):
        #     if i == 0:
        #         out = self.regressor(dropout(x))
        #     else:
        #         out += self.regressor(dropout(x))
        # out /= len(self.dropouts)

        if self.hparams.use_hidden:
            states = model_out[2]
            out = torch.stack(
                tuple(states[-i - 1] for i in range(self.config.num_hidden_layers)),
                dim=0,
            )
            out_mean = torch.mean(out, dim=0)
            out_max, _ = torch.max(out, dim=0)
            out = torch.cat((out_mean, out_max), dim=-1)
        else:
            out = model_out[0]

        out = self.seq_attn_head(out)
        out = torch.cat([out, features], -1)
        out = self.regressor(out)

        if out.shape[1] == 1:
            return out, None
        else:
            mean = out[:, 0].view(-1, 1)
            log_var = out[:, 1].view(-1, 1)
            return mean, log_var

    def training_step(self, batch, batch_idx):
        inputs, labels, features = batch
        mean, log_var = self.forward(features, **inputs)
        if self.hparams.kl_loss:
            p = torch.distributions.Normal(mean, torch.exp(log_var))
            q = torch.distributions.Normal(labels["target"], labels["error"])
            loss = torch.distributions.kl_divergence(p, q).mean()
        else:
            loss = self.loss_fn(mean, labels["target"])
        self.log_dict({"loss/train_step": loss})
        return {"loss": loss}

    def training_epoch_end(self, training_step_outputs):
        avg_loss = torch.stack([x["loss"] for x in training_step_outputs]).mean()
        self.log("loss/train", avg_loss, sync_dist=True)

    def validation_step(self, batch, batch_idx):
        inputs, labels, features = batch
        mean, log_var = self.forward(features, **inputs)
        if self.hparams.kl_loss:
            p = torch.distributions.Normal(mean, torch.exp(log_var))
            q = torch.distributions.Normal(labels["target"], labels["error"])
            loss = torch.distributions.kl_divergence(p, q).mean()
        else:
            loss = self.loss_fn(mean, labels["target"])

        return {
            "val_loss": loss,
            "y_pred": mean,
            "y_true": labels["target"],
        }

    def validation_epoch_end(self, outputs):
        loss_val = torch.stack([x["val_loss"] for x in outputs]).mean()
        y_pred = torch.cat([x["y_pred"] for x in outputs])
        y_true = torch.cat([x["y_true"] for x in outputs])

        rmse = torch.sqrt(self.loss_fn(y_pred, y_true))

        self.log_dict(
            {
                "loss/valid": loss_val,
                "rmse": rmse,
            },
            prog_bar=True,
            sync_dist=True,
        )

    # learning rate warm-up
    def optimizer_step(
        self,
        epoch,
        batch_idx,
        optimizer,
        optimizer_idx,
        optimizer_closure,
        on_tpu=False,
        using_native_amp=False,
        using_lbfgs=False,
    ):
        # Warm-up the first 100 steps
        if self.trainer.global_step < self.hparams.warmup:
            lr_scale = min(
                1.0, float(self.trainer.global_step + 1) / self.hparams.warmup
            )
            for pg in optimizer.param_groups:
                pg["lr"] = lr_scale * self.hparams.lr

        # update params
        optimizer.step(closure=optimizer_closure)

    def configure_optimizers(self):
        parameters = add_weight_decay(
            self,
            self.hparams.weight_decay,
            skip_list=["bias", "LayerNorm.bias", "LayerNorm.weight"],
        )

        opt = AdamW(
            parameters,
            lr=self.hparams.lr,
            betas=self.hparams.betas,
            eps=self.hparams.eps,
        )

        sch = torch.optim.lr_scheduler.CosineAnnealingLR(
            opt, T_max=1000, eta_min=self.hparams.lr / 10
        )

        return {
            "optimizer": opt,
            "lr_scheduler": {"scheduler": sch, "interval": "step"},
        }


# utils.py
def add_weight_decay(model, weight_decay=1e-5, skip_list=()):
    decay = []
    no_decay = []
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
        if len(param.shape) == 1 or any(s in name for s in skip_list):
            no_decay.append(param)
        else:
            decay.append(param)
    return [
        {"params": no_decay, "weight_decay": 0.0},
        {"params": decay, "weight_decay": weight_decay},
    ]


# datasets.py
class CommonLitDataset(Dataset):
    def __init__(self, df, tokenizer, max_len=256):
        self.df = df.reset_index(drop=True)
        self.excerpt = self.df["excerpt"]
        self.tokenizer = tokenizer
        self.max_len = max_len

    def __len__(self):
        return len(self.excerpt)

    def __getitem__(self, index):
        row = self.df.loc[index]
        inputs = self.tokenizer(
            str(row["excerpt"]),
            max_length=self.max_len,
            padding="max_length",
            truncation=True,
            # add_special_tokens=True  # not sure what this does
        )

        input_dict = {
            "input_ids": torch.tensor(inputs["input_ids"], dtype=torch.long),
            "attention_mask": torch.tensor(inputs["attention_mask"], dtype=torch.long),
        }

        if "target" in self.df.columns:
            labels = {
                "target": torch.tensor([row["target"]], dtype=torch.float32),
                "error": torch.tensor([row["standard_error"]], dtype=torch.float32),
            }

            # For id 436ce79fe
            if labels["error"] <= 0:
                labels["error"] += 0.5

            labels["target_stoch"] = torch.normal(
                mean=labels["target"], std=labels["error"]
            )
        else:
            labels = 0

        # Add addtional features
        features = self.generate_features(str(row["excerpt"]))

        return input_dict, labels, features

    def generate_features(self, text):
        means = torch.tensor([67.742121, 10.308363])
        stds = torch.tensor([17.530230, 3.298237])
        features = torch.tensor(
            [
                # textstat.sentence_count(text),
                # textstat.lexicon_count(text),
                textstat.flesch_reading_ease(text),
                textstat.smog_index(text),
            ]
        )
        return (features - means) / stds


# infer.py
def infer(model, dataset, batch_size=32, device="cuda"):
    model.to(device)
    model.eval()
    loader = DataLoader(dataset, batch_size=batch_size, num_workers=1)

    predictions = []
    with torch.no_grad():
        for input_dict, _, features in loader:
            input_dict = {k: v.to(device) for k, v in input_dict.items()}
            mean, log_var = model(features.to(device), **input_dict)
            predictions.append(mean.cpu())

    return torch.cat(predictions, 0)


# https://kaggler.readthedocs.io/en/latest/_modules/kaggler/ensemble/linear.html#netflix
def netflix(es, ps, e0, l=0.0001):
    """Combine predictions with the optimal weights to minimize RMSE.

    Ref: Töscher, A., Jahrer, M., & Bell, R. M. (2009). The bigchaos solution to the netflix grand prize.

    Args:
        es (list of float): RMSEs of predictions
        ps (list of np.array): predictions
        e0 (float): RMSE of all zero prediction
        l (float): lambda as in the ridge regression

    Returns:
        (tuple):

            - (np.array): ensemble predictions
            - (np.array): weights for input predictions
    """
    m = len(es)
    n = len(ps[0])

    X = np.stack(ps).T
    pTy = 0.5 * (n * e0 ** 2 + (X ** 2).sum(axis=0) - n * np.array(es) ** 2)

    w = np.linalg.pinv(X.T.dot(X) + l * n * np.eye(m)).dot(pTy)

    return X.dot(w), w


def create_folds(data, y, n_splits=5, random_state=None):
    data = data.sample(frac=1, random_state=random_state).reset_index(drop=True)
    num_bins = int(np.floor(1 + np.log2(len(data))))
    data.loc[:, "bins"] = pd.cut(y, bins=num_bins, labels=False)
    kf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state)

    splits = []
    for f, (t_, v_) in enumerate(kf.split(X=data, y=data.bins.values)):
        splits.append((t_, v_))
    return splits


def make_predictions(dataset_paths, device="cuda"):
    mpaths, oof_paths = [], []
    for p in dataset_paths:
        mpaths.append(sorted(list(p.rglob(f"*.ckpt"))))
        oof_paths.extend(sorted(list(p.glob(f"*.csv"))))

    print(
        f"{len([item for sublist in mpaths for item in sublist])} models found.",
        f"{len(oof_paths)} OOFs found",
    )

    # Construct OOF df
    oofs = pd.read_csv(INPUT_PATH / "train.csv", usecols=["id", "target"]).sort_values(
        by="id"
    )
    for i, p in enumerate(oof_paths):
        x = pd.read_csv(p).sort_values(by="id")
        oofs[f"model_{i}"] = x["prediction"].values

    df = pd.read_csv(INPUT_PATH / "test.csv")
    output = 0

    # print(mpaths)

    for i, group in enumerate(mpaths):
        output = 0
        for p in group:
            print(p)
            config = AutoConfig.from_pretrained(str(p.parent))
            tokenizer = AutoTokenizer.from_pretrained(str(p.parent))
            model = CommonLitModel.load_from_checkpoint(p, hf_config=config)
            dataset = CommonLitDataset(df, tokenizer)
            output += infer(model, dataset, device=device)

            del model
            del dataset
            del tokenizer
            gc.collect()

        df[f"model_{i}"] = output.squeeze().numpy() / len(group)

    pred_cols = [f"model_{i}" for i in range(len(mpaths))]

    # Stack using Netflix method
    oof_preds = [oofs[c].values for c in pred_cols]
    rmses = [np.sqrt(mean_squared_error(p, oofs["target"])) for p in oof_preds]
    ensemble, weights = netflix(rmses, oof_preds, 1.4100)
    score = np.sqrt(mean_squared_error(ensemble, oofs["target"]))
    print(f"Best RMSE: {score:0.5f}")
    df["target"] = df[pred_cols] @ weights

    df[["id", "target"]].to_csv("submission.csv", index=False)


if __name__ == "__main__":

    model_folders = [
        "20210628-145921",
        "20210624-150250v2",
        "20210616-041221",
        "20210623-232231",
        "20210624-012102",
        "20210619-004022",
        "20210617-135233",
        "20210619-035747",
        "20210624-101855",
        "20210628-045559",
        "20210618-223208",
        "20210624-015812",
        "20210627-105144",
        "20210627-152616",
        "20210624-113506",
        "20210615-094729",
        "20210624-044356",
        "20210614-203831",
        "20210622-152356",
        "20210628-085322",
        "20210627-213946",
        "20210617-120949",
        "20210619-064351",
        "20210623-201514",
        "20210618-183719",
        "20210628-031447",
        "20210629-183058",
        "20210627-151904",
        "20210616-003038",
        "20210616-132341",
        "20210629-012726",
        "20210629-163239",
        "20210629-035901",
        "20210628-212819",
        #     "20210627-105133",
        #     "20210627-195827",
        #     "20210619-093050",
        #     "20210629-224305",
    ]

    if KERNEL:
        dataset_paths = [
            Path(f"../input/commonlitreadabilityprize-{f}") for f in model_folders
        ]
    else:
        dataset_paths = [OUTPUT_PATH / f for f in model_folders]

    predictions = make_predictions(dataset_paths, device="cuda")
