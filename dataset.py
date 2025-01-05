import torch
from torch.utils.data import Dataset, DataLoader
import tiktoken
import numpy as np
import os
from config import DATA_ROOT
from fineweb import TOTAL_TOKENS    # total no. of tokens in FineWebEdu sample-10BT

class TinyShakespeare(Dataset):
    """
    Implements both overlapping and non-overlapping samples within batches.

    If `batch_size=None`, overlapping sampling advances by a `+1` sliding window
    to create `(self.tokens.shape[0] - self.block_size) / self.batch_size` samples.

    With a specified `batch_size`, chunk sampling will advance by an index of
    `self.batch_size * self.block_size` with manual batch construction using
    `.view(self.batch_size, -1)`. The `batch_size` parameter WITHIN a `DataLoader`
    object must be set to `None` if using chunk sampling.

    Dataset link: https://raw.githubusercontent.com/karpathy/char-rnn/master/data/tinyshakespeare/input.txt
    """

    def __init__(self, block_size: int, batch_size=None, pct=1, split="train", train_split=0.9, verbose=True):
        assert split.lower() in ["train", "val"]
        with open("input.txt", "r") as f:   # in the same directory
            text = f.read()
        enc = tiktoken.get_encoding("gpt2")
        self.data = torch.tensor(enc.encode(text), dtype=torch.long)
        self.data = self.data[:int(pct * len(self.data))]
        self.block_size = block_size    # context length
        n_train = int(train_split * len(self.data))
        self.tokens = self.data[:n_train] if split == "train" else self.data[n_train:]
        if verbose:
            n_test = len(self.data) - n_train
            print(f"no. of tokens: {len(self.data):,} ({pct * 100:.0f}% of full data)")
            print(f"train/val split: {n_train:,} ({train_split * 100:.0f}%), {n_test:,} ({(1 - train_split) * 100:.0f}%)")
            print(f"sequence (context) length: {block_size:,} tokens")
        # ---
        self.batch_size = batch_size

    def __len__(self):
        if self.tokens.shape[0] == 0:   # if no items (only for 0 % validation split)
            return 0
        if self.batch_size is None:
            return self.tokens.shape[0] - self.block_size   # sliding window (overlapping samples)
        return (self.tokens.shape[0] - self.block_size) // (self.block_size * self.batch_size)

    def __getitem__(self, idx):
        if self.batch_size is None:
            X = self.tokens[idx: idx + self.block_size]
            y = self.tokens[idx + 1: idx + self.block_size + 1]
            return X, y
        # carry out MANUAL BATCHING (set batch_size=None in the DataLoader object)
        chunk = self.batch_size * self.block_size
        curr = idx * chunk      # position based on idx (previously removed statefullness)
        X = self.tokens[curr: curr + chunk]
        y = self.tokens[curr + 1: curr + chunk + 1]
        return X.view(self.batch_size, -1), y.view(self.batch_size, -1)
    

class FineWebEdu(Dataset):
    """
    Loads the FineWeb-Edu (`sample-10BT`) dataset shards for training and validation splits.
    Handles iterable batching through chunk samples of `block_size * batch_size` tokens across all shards.
    """

    def __init__(self, batch_size: int, block_size: int, process_rank: int, world_size: int, split="train", dir=DATA_ROOT, verbose=True):
        self.verbose = verbose
        assert split.lower() in ["train", "val"], "split must be either 'train' or 'val'"
        self.batch_size = batch_size        # no. of samples to user in a forward pass
        self.block_size = block_size        # context (sequence) length
        self.rank       = process_rank      # process integer ID (e.g. 0-7 for 8 GPUs)
        self.world_size = world_size        # total no. of processes (i.e. no of GPUs)
        self.root       = dir               # specify directory where the data shards are stored in config.py
        self.shards = self._get_shard_paths(split)    # load shards from directory based on split
        # initialise the shard index, load the first shard, and set the starting local index:
        self.shard_idx  = 0
        self.tokens     = self._load_shard(self.shard_idx)
        self.idx        = self.batch_size * self.block_size * self.rank    # set the starting LOCAL index based on the process rank

    def _get_shard_paths(self, split):
        """Get shard file names from the root directory (based on the split) to construct their full paths."""
        names = [f for f in os.listdir(self.root) if split in f]       # get individual shard file names
        shards = [os.path.join(self.root, f) for f in names]               # construct full paths, sorted by name (ascending order)
        assert len(shards) > 0, f"no shards found for split='{split}' in {self.root}"
        if self.verbose and self.rank == 0:
            print(f"found {len(shards):,} shards for '{split}' split")
        return shards   # return list of full paths to shards

    def _load_shard(self, shard_idx: int):
        """Loads a single shard as a PyTorch tensor of tokens, based on the `shard_idx`."""
        path = self.shards[shard_idx]       # get the full shard path
        tokens = np.load(path)              # load the shard as a numpy array
        return torch.tensor(tokens, dtype=torch.long)   # convert to PyTorch tensor with int64 dtype
    
    def __getitem__(self, idx):
        chunk_size = self.batch_size * self.block_size                  # chunk size (tokens per batch) for each batch
        if self.idx + chunk_size >= self.tokens.shape[0]:               # if the current shard is exhausted
            X1 = self.tokens[self.idx:]                                 # get any remaining tokens of current shard
            y1 = self.tokens[self.idx + 1:]                             # corresponding target sequence (next token for each sample)
            self.shard_idx = (self.shard_idx + 1) % len(self.shards)    # move to the next shard (circular indexing)
            self.tokens = self._load_shard(self.shard_idx)              # load the next shard
            X2 = self.tokens[: chunk_size - X1.shape[0]]                # get the remaining tokens to complete the chunk
            y2 = self.tokens[: chunk_size - y1.shape[0] + 1]            # corresponding target sequence
            X = torch.cat((X1, X2), dim=0)                              # concatenate the two parts
            y = torch.cat((y1, y2), dim=0)                              # concatenate the two parts       
            self.idx = chunk_size - X2.shape[0]                         # set the new local index
        else:
            X = self.tokens[self.idx: self.idx + chunk_size]            # get the input sequence
            y = self.tokens[self.idx + 1: self.idx + chunk_size + 1]    # get the target sequence (next token for each sample)
            self.idx += chunk_size * self.world_size                    # advance local index by one chunk size (across all processes)
        return X.view(self.batch_size, -1), y.view(self.batch_size, -1) # return the input and target sequences

    def __len__(self):
        """Return the number of available batches in one epoch."""
        return TOTAL_TOKENS // (self.batch_size * self.block_size * self.world_size)


if __name__ == "__main__":

    # ----- DataLoader EXAMPLES with TinyShakespeare ----- #

    batch_size = 16             # samples per forward pass
    block_size = 1024           # context length
    # chunk_sampling = False      # batch processing method

    # if chunk_sampling:
    #     print(f"\nutilising chunk sampling (non-overlapping batches)")
    #     train_loader = DataLoader(
    #         TinyShakespeare(block_size, batch_size=batch_size),
    #         batch_size=None,    # must be set to None
    #         shuffle=False,
    #     )
    # else:
    #     print(f"\nutilising overlapping samples across batches")
    #     train_loader = DataLoader(
    #         TinyShakespeare(block_size),    # no specified batch_size within Dataset class
    #         batch_size=batch_size,    # specify DataLoader batch size parameter
    #         shuffle=False
    #     )
    # print(f"\ntokens per batch: {batch_size * block_size:,} (batch size {batch_size:,})")
    # print(f"{len(train_loader):,} available batches per epoch")
    
    # X, y = next(iter(train_loader))
    # print(X.shape, y.shape)     # shape --> [batch_size, block_size]

    # ----- DataLoader EXAMPLES with FineWebEdu Sample-10BT ----- #

    print(f"creating DataLoader for FineWebEdu Sample-10BT dataset..\n")
    train_loader = DataLoader(
        FineWebEdu(block_size=1024, batch_size=16, process_rank=0, world_size=8, split="train"),
        batch_size=None,    # must be set to None
        shuffle=False,
    )

    print(f"\ntokens per batch: {batch_size * block_size:,} (batch size {batch_size:,})")
    print(f"{len(train_loader):,} available batches per epoch")
    
    X, y = next(iter(train_loader))
    print(X.shape, y.shape)     # shape --> [batch_size, block_size]

