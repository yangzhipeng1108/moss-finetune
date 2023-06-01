"""Code for moss-sft"""

import os
import copy
import json
import torch
import logging
import argparse

import torch.distributed as dist
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm
from accelerate import Accelerator
from torch.utils.data import Dataset, DataLoader
from torch.utils.tensorboard import SummaryWriter
from transformers import set_seed, get_cosine_schedule_with_warmup,get_polynomial_decay_schedule_with_warmup,get_cosine_with_hard_restarts_schedule_with_warmup

from transformers import AutoTokenizer, AutoModelForCausalLM

logger = logging.getLogger(__name__)
logging.basicConfig(level='INFO')
from torch.nn.parallel import DistributedDataParallel as DDP

class SFTDataset(Dataset):
    def __init__(self, data_dir, tokenizer, data_type='train'):
        super().__init__()

        self.data_dir = data_dir
        self.tokenizer = tokenizer
        self.data_type = data_type

        self.data = []
        # We do not calculate losses for the meta instruction or results returned by plugins
        # The token spans with label -100, [(span_start, span_end), ...]
        self.no_loss_spans = []

        self.load_data()

    def load_data(self):
        logger.info("Loading data...")
        data_file = os.path.join(self.data_dir, f'{self.data_type}_data')
        no_loss_spans_file = os.path.join(self.data_dir, f'{self.data_type}_no_loss_spans')
        if os.path.exists(data_file) and os.path.exists(no_loss_spans_file):
            self.data = torch.load(data_file, map_location='cpu')
            self.no_loss_spans = torch.load(no_loss_spans_file, map_location='cpu')
        else:
            with open(os.path.join(self.data_dir, f'{self.data_type}.jsonl'), 'r') as f:
                for line in f:
                    sample = json.loads(line)

                    chat = sample['chat']
                    num_turns = int(sample['num_turns'])

                    meta_instruction = sample['meta_instruction']
                    instruction_ids = self.tokenizer.encode(meta_instruction)
                    assert isinstance(instruction_ids, list) and len(instruction_ids) > 0
                    
                    input_ids = copy.deepcopy(instruction_ids)
                    no_loss_spans = [(0, len(instruction_ids))]

                    for i in range(num_turns):
                        cur_turn_ids = []
                        cur_no_loss_spans = []
                        cur_turn = chat[f'turn_{i+1}']
                        for key, value in cur_turn.items():

                            cur_ids = self.tokenizer.encode(value)

                            if key == 'Tool Responses':
                                # The format tokens (<|Results|>:...<eor>\n) should have losses. 
                                cur_no_loss_spans.append((len(input_ids + cur_turn_ids) + 5, len(input_ids + cur_turn_ids + cur_ids) - 2))    

                            assert isinstance(cur_ids, list) and len(cur_ids) > 0

                            cur_turn_ids.extend(cur_ids)

                        if len(input_ids + cur_turn_ids) > 2048:
                            break

                        input_ids.extend(cur_turn_ids)
                        no_loss_spans.extend(cur_no_loss_spans)

                    if len(input_ids) == len(instruction_ids):
                        continue

                    assert len(input_ids) > 0 and len(input_ids) <= 2048

                    self.data.append(input_ids)
                    self.no_loss_spans.append(no_loss_spans)
            
            torch.save(self.data, data_file)
            torch.save(self.no_loss_spans, no_loss_spans_file)

        logger.info(f"Load data successfully, total {len(self.data)} training samples")

    def __len__(self):
        return len(self.data)
    
    def __getitem__(self, index):
        data = copy.deepcopy(self.data[index])
        no_loss_spans = copy.deepcopy(self.no_loss_spans[index])
        
        data = torch.tensor(data, dtype=torch.long)
        attn_mask = torch.ones_like(data, dtype=torch.bool)
        label = copy.deepcopy(data)

        for no_loss_span in no_loss_spans:
            label[no_loss_span[0] : no_loss_span[1]] = -100

        return data, attn_mask, label
    
    def collate_fn(self, batch):
        batch_input_ids, batch_attn_mask, batch_labels = [], [], []
        for input_ids, attn_mask, label in batch:
            batch_input_ids.append(input_ids)
            batch_attn_mask.append(attn_mask)
            batch_labels.append(label)

        batch_input_ids = torch.nn.utils.rnn.pad_sequence(batch_input_ids, batch_first=True, padding_value=self.tokenizer.eos_token_id)
        batch_attn_mask = torch.nn.utils.rnn.pad_sequence(batch_attn_mask, batch_first=True, padding_value=0).to(torch.bool)
        batch_labels = torch.nn.utils.rnn.pad_sequence(batch_labels, batch_first=True, padding_value=-100)

        return batch_input_ids, batch_attn_mask, batch_labels
    

class SFTMetric:
    def __init__(self, device):
        self.n_step = 0
        self.right = torch.Tensor([0]).to(device=device)
        self.total = torch.Tensor([0]).to(device=device)
        self.total_loss = torch.Tensor([0]).to(device=device)
        self.world_size = dist.get_world_size()

    def __call__(self, logits, labels, loss):
        return self.update(logits, labels, loss)

    def update(self, logits, labels, loss):
        self.n_step += 1
        with torch.no_grad():
            shift_preds = logits[..., :-1, :].argmax(dim=-1)
            shift_labels = labels[..., 1:]
            self.right += (shift_preds == shift_labels).masked_fill(shift_labels.eq(-100), 0).sum().item()
            self.total += (shift_labels != -100).sum().item()
            self.total_loss += loss.item()

    def get_metric(self, reset=True):
        dist.all_reduce(self.right, op=torch.distributed.ReduceOp.SUM)
        dist.all_reduce(self.total, op=torch.distributed.ReduceOp.SUM)
        dist.all_reduce(self.total_loss, op=torch.distributed.ReduceOp.SUM)

        acc = (self.right / self.total).item()
        loss = self.total_loss.item() / (self.world_size * self.n_step)

        if reset:
            self.n_step = 0
            self.right.fill_(0)
            self.total.fill_(0)
            self.total_loss.fill_(0)
        return acc, loss
    

def train(args):

    args.global_rank = int(os.environ["RANK"])
    args.local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(args.global_rank % torch.cuda.device_count())
    dist.init_process_group(backend="nccl")

    torch.cuda.set_device(args.local_rank)
    device = torch.device("cuda", args.local_rank)

    torch.distributed.barrier()

    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, trust_remote_code=True)
    tokenizer.eos_token_id = 106068 # The eos_token_id of base model is 106028. We need map the eos token to <eom> (its token id is 106068)

    model = AutoModelForCausalLM.from_pretrained(args.model_name_or_path, trust_remote_code=True, use_cache=False)

    model.transformer.gradient_checkpointing = True
    assert model.transformer.gradient_checkpointing is True

    model = model.to(device)
    # DistributedDataParallel
    model = DDP(model, device_ids=[args.local_rank], output_device=args.local_rank)


    # Optimizer
    # Split weights in two groups, one with weight decay and the other not.
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]


    optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate,eps= 0.000015)

    train_dataset = SFTDataset(args.data_dir, tokenizer)
    train_sampler = DistributedSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, batch_size=args.train_bsz_per_gpu, sampler=train_sampler,collate_fn=train_dataset.collate_fn)

    val_dataset = SFTDataset(args.data_dir, tokenizer, data_type='val')
    val_sampler = DistributedSampler(val_dataset)
    val_dataloader = DataLoader(val_dataset, batch_size=args.eval_bsz_per_gpu, sampler=val_sampler, collate_fn=train_dataset.collate_fn)

    num_training_steps = (len(train_dataloader) * args.n_epochs)
    lr_scheduler = get_cosine_schedule_with_warmup(optimizer, num_warmup_steps=int(args.warmup_rates * num_training_steps), num_training_steps=num_training_steps)

    global_step = 0
    metric = SFTMetric(device=device)
    
    model.train()
    for epoch in range(args.n_epochs):
        for batch_cnt, (input_ids, attention_mask, labels) in enumerate(train_dataloader):
            if batch_cnt == 1 and epoch == 0:
                torch.cuda.empty_cache()

            optimizer.zero_grad()
            input_ids=input_ids.to(device)
            attention_mask=attention_mask.to(device)
            labels=labels.to(device)
            
            output = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels, return_dict=True)
            loss = output.loss

            metric(output.logits, labels, loss)
            acc, train_loss = metric.get_metric()

            loss.backward()
            # torch.nn.utils.clip_grad_value_(model.parameters(),0.5)
            optimizer.step()
            lr_scheduler.step()

            global_step += 1

            print(f"epoch: {epoch}, cureent step: {batch_cnt}, total step: {len(train_dataloader)}, loss:{round(train_loss, 3)}, acc:{round(acc, 3)}, length:{len(input_ids[0])}, lr:{lr_scheduler.get_last_lr()[0]}")

            if global_step % args.eval_step == 0 or global_step == 1:
                torch.cuda.empty_cache()
                model.eval() 

                val_metric = SFTMetric(torch.cuda.current_device())
                for input_ids, attention_mask, labels in val_dataloader:
                    input_ids = input_ids.to(device)
                    attention_mask = attention_mask.to(device)
                    labels = labels.to(device)
                    with torch.no_grad():
                        output = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels, return_dict=True)

                    val_metric(output.logits, labels, output.loss)

                val_acc, val_loss = val_metric.get_metric()

                print(f"Epoch: {epoch}, Step: {batch_cnt}, Val loss: {val_loss}, Val acc: {val_acc}")

                model.train()           

            if global_step % args.save_step == 0 and args.local_rank == 0:
                torch.save(model.module.state_dict(), os.path.join(args.output_dir ,'pytorch_model.bin'))


    if global_step % args.save_step != 0 and args.local_rank == 0: #accelerator.is_main_process
        torch.save(model.module.state_dict(), os.path.join(args.output_dir ,'pytorch_model.bin'))
        

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Args of sft')

    # Model Args
    parser.add_argument('--model_name_or_path', default='./ckpts/moss-16B-base', type=str)
    
    # Data Args
    parser.add_argument('--data_dir', default='./data/sft', type=str)
    parser.add_argument('--output_dir', default='./ckpts/moss-16B-sft', type=str)
    parser.add_argument('--log_dir', default='./train_logs/moss-16B-sft', type=str)
    
    # Training Args
    parser.add_argument('--max_seq_len', default=2048, type=int)
    parser.add_argument('--train_bsz_per_gpu', default=4, type=int)
    parser.add_argument('--eval_bsz_per_gpu', default=4, type=int)
    parser.add_argument('--weight_decay', default=0.1, type=float)
    parser.add_argument('--learning_rate', default=9e-6, type=float)
    parser.add_argument('--warmup_rates', default=0.05, type=int)
    parser.add_argument('--n_epochs', default=2, type=int)

    # Other Args
    parser.add_argument('--save_step', default=3000, type=int)
    parser.add_argument('--eval_step', default=5, type=int)
    parser.add_argument('--seed', default=42, type=int)

    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")

    args = parser.parse_args()


    os.makedirs(args.log_dir, exist_ok=True)
    os.makedirs(args.output_dir, exist_ok=True)

    set_seed(args.seed)
    train(args)           
