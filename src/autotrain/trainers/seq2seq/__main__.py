import argparse
import json
import os
from functools import partial

import pandas as pd
import torch
from accelerate import Accelerator
from accelerate.state import PartialState
from datasets import Dataset, load_dataset
from huggingface_hub import HfApi
from peft import LoraConfig, TaskType, get_peft_model, prepare_model_for_int8_training
from transformers import (
    AutoConfig,
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    DataCollatorForSeq2Seq,
    EarlyStoppingCallback,
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
)

from autotrain import logger
from autotrain.trainers.seq2seq import utils
from autotrain.trainers.seq2seq.dataset import Seq2SeqDataset
from autotrain.trainers.seq2seq.params import Seq2SeqParams
from autotrain.utils import monitor


def parse_args():
    # get training_config.json from the end user
    parser = argparse.ArgumentParser()
    parser.add_argument("--training_config", type=str, required=True)
    return parser.parse_args()


@monitor
def train(config):
    if isinstance(config, dict):
        config = Seq2SeqParams(**config)

    if config.repo_id is None and config.username is not None:
        config.repo_id = f"{config.username}/{config.project_name}"

    if PartialState().process_index == 0:
        logger.info("Starting training...")
        logger.info(f"Training config: {config}")

    train_data = None
    valid_data = None
    # check if config.train_split.csv exists in config.data_path
    if config.train_split is not None:
        train_path = f"{config.data_path}/{config.train_split}.csv"
        if os.path.exists(train_path):
            logger.info("loading dataset from csv")
            train_data = pd.read_csv(train_path)
            train_data = Dataset.from_pandas(train_data)
        else:
            train_data = load_dataset(
                config.data_path,
                split=config.train_split,
                token=config.token,
            )

    if config.valid_split is not None:
        valid_path = f"{config.data_path}/{config.valid_split}.csv"
        if os.path.exists(valid_path):
            logger.info("loading dataset from csv")
            valid_data = pd.read_csv(valid_path)
            valid_data = Dataset.from_pandas(valid_data)
        else:
            valid_data = load_dataset(
                config.data_path,
                split=config.valid_split,
                token=config.token,
            )

    model_config = AutoConfig.from_pretrained(config.model, token=config.token, trust_remote_code=True)

    if config.use_peft:
        # if config.use_int4:
        #     bnb_config = BitsAndBytesConfig(
        #         load_in_4bit=config.use_int4,
        #         bnb_4bit_quant_type="nf4",
        #         bnb_4bit_compute_dtype=torch.float16,
        #         bnb_4bit_use_double_quant=False,
        #     )
        #     config.fp16 = True
        if config.use_int8:
            bnb_config = BitsAndBytesConfig(load_in_8bit=config.use_int8)
            config.fp16 = True
        else:
            bnb_config = None

        model = AutoModelForSeq2SeqLM.from_pretrained(
            config.model,
            config=model_config,
            token=config.token,
            quantization_config=bnb_config,
            torch_dtype=torch.float16,
            device_map={"": Accelerator().process_index} if torch.cuda.is_available() else None,
            trust_remote_code=True,
        )
    else:
        model = AutoModelForSeq2SeqLM.from_pretrained(
            config.model,
            config=model_config,
            token=config.token,
            trust_remote_code=True,
        )

    tokenizer = AutoTokenizer.from_pretrained(config.model, token=config.token, trust_remote_code=True)

    embedding_size = model.get_input_embeddings().weight.shape[0]
    if len(tokenizer) > embedding_size:
        model.resize_token_embeddings(len(tokenizer))

    if config.use_peft:
        lora_config = LoraConfig(
            r=config.lora_r,
            lora_alpha=config.lora_alpha,
            target_modules=None if len(config.target_modules) == 0 else config.target_modules,
            lora_dropout=config.lora_dropout,
            bias="none",
            task_type=TaskType.SEQ_2_SEQ_LM,
        )
        if config.use_int8:
            model = prepare_model_for_int8_training(model)

        model = get_peft_model(model, lora_config)

    train_data = Seq2SeqDataset(data=train_data, tokenizer=tokenizer, config=config)
    if config.valid_split is not None:
        valid_data = Seq2SeqDataset(data=valid_data, tokenizer=tokenizer, config=config)

    if config.logging_steps == -1:
        if config.valid_split is not None:
            logging_steps = int(0.2 * len(valid_data) / config.batch_size)
        else:
            logging_steps = int(0.2 * len(train_data) / config.batch_size)
        if logging_steps == 0:
            logging_steps = 1

    else:
        logging_steps = config.logging_steps

    training_args = dict(
        output_dir=config.project_name,
        per_device_train_batch_size=config.batch_size,
        per_device_eval_batch_size=2 * config.batch_size,
        learning_rate=config.lr,
        num_train_epochs=config.epochs,
        fp16=config.fp16,
        evaluation_strategy=config.evaluation_strategy if config.valid_split is not None else "no",
        logging_steps=logging_steps,
        save_total_limit=config.save_total_limit,
        save_strategy=config.save_strategy,
        gradient_accumulation_steps=config.gradient_accumulation,
        report_to="tensorboard",
        auto_find_batch_size=config.auto_find_batch_size,
        lr_scheduler_type=config.scheduler,
        optim=config.optimizer,
        warmup_ratio=config.warmup_ratio,
        weight_decay=config.weight_decay,
        max_grad_norm=config.max_grad_norm,
        push_to_hub=False,
        load_best_model_at_end=True if config.valid_split is not None else False,
        ddp_find_unused_parameters=False,
        predict_with_generate=True,
        seed=config.seed,
    )

    if config.valid_split is not None:
        early_stop = EarlyStoppingCallback(early_stopping_patience=3, early_stopping_threshold=0.01)
        callbacks_to_use = [early_stop]
    else:
        callbacks_to_use = []

    args = Seq2SeqTrainingArguments(**training_args)
    _s2s_metrics = partial(utils._seq2seq_metrics, tokenizer=tokenizer)
    trainer_args = dict(
        args=args,
        model=model,
        callbacks=callbacks_to_use,
        compute_metrics=_s2s_metrics,
    )
    data_collator = DataCollatorForSeq2Seq(tokenizer, model=model)
    trainer = Seq2SeqTrainer(
        **trainer_args,
        train_dataset=train_data,
        eval_dataset=valid_data,
        data_collator=data_collator,
        tokenizer=tokenizer,
    )

    trainer.train()

    logger.info("Finished training, saving model...")
    trainer.save_model(config.project_name)
    tokenizer.save_pretrained(config.project_name)

    model_card = utils.create_model_card(config, trainer)

    # remove token key from training_params.json located in output directory
    # first check if file exists
    if os.path.exists(f"{config.project_name}/training_params.json"):
        training_params = json.load(open(f"{config.project_name}/training_params.json"))
        if "token" in training_params:
            training_params.pop("token")
            json.dump(
                training_params,
                open(f"{config.project_name}/training_params.json", "w"),
            )

    # save model card to output directory as README.md
    with open(f"{config.project_name}/README.md", "w") as f:
        f.write(model_card)

    if config.push_to_hub:
        if PartialState().process_index == 0:
            logger.info("Pushing model to hub...")
            api = HfApi(token=config.token)
            api.create_repo(repo_id=config.repo_id, repo_type="model", private=True)
            api.upload_folder(
                folder_path=config.project_name,
                repo_id=config.repo_id,
                repo_type="model",
            )

    if PartialState().process_index == 0:
        if "SPACE_ID" in os.environ:
            # shut down the space
            logger.info("Pausing space...")
            api = HfApi(token=config.token)
            api.pause_space(repo_id=os.environ["SPACE_ID"])

        if "ENDPOINT_ID" in os.environ:
            # shut down the endpoint
            logger.info("Pausing endpoint...")
            utils.pause_endpoint(config)


if __name__ == "__main__":
    args = parse_args()
    training_config = json.load(open(args.training_config))
    config = Seq2SeqParams(**training_config)
    train(config)
