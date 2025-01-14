import torch
from torch.utils.data import Dataset, DataLoader, DistributedSampler
import tiktoken
import numpy as np
import os
from config import DATA_ROOT
from fineweb import SHARD_SIZE    

LAST_SHARD_SIZE = 82_590_278    # no. of tokens in the last shard from downloading FineWeb-Edu (sample-10BT)

class FineWebEdu(Dataset):
    """
    PyTorch Dataset for FineWeb-Edu (`sample-10BT`) dataset shards.
    ---
    Handles the loading of tokenized dataset shards stored as NumPy arrays in `DATA_ROOT`.
    Supports batching of data within a shard file as well as across shard boundaries.
    Returns input (`X`) and target (`y`) with shape `[batch_size, block_size]`.

    `__len__()` method:
    ---
    Returns the number of available batches in one epoch for a given `split`.
    Only the first shard with `100M` tokens (`SHARD_SIZE`) is used for the validation set.
    The training set uses the remaining `99` shards. 
    NOTE: The first `98` shards of the training set hold `100M` tokens each,
    while the last shard holds exactly `82,590,278` tokens.
    """

    def __init__(self, batch_size: int, block_size: int, split="train", dir=DATA_ROOT, verbose=True):
        self.verbose = verbose
        assert split.lower() in ["train", "val"], "split must be either 'train' or 'val'"
        self.split = split              # split to specify __len__() method
        self.batch_size = batch_size    # no. of samples to user in a forward pass
        self.block_size = block_size    # context (sequence) length
        self.root       = dir           # specify directory where the data shards are stored in config.py
        self.shards = self._get_shard_paths(split)    # load shards from directory based on split
        self.cache      = {}            # cache for validation set only

    def _get_shard_paths(self, split):
        """Get shard file names from the root directory (based on the split) to construct their full paths."""
        names = [f for f in os.listdir(self.root) if split in f]       # get individual shard file names
        shards = [os.path.join(self.root, f) for f in names]               # construct full paths, sorted by name (ascending order)
        assert len(shards) > 0, f"no shards found for split='{split}' in {self.root}"
        if self.verbose:
            if split == "val":      # validation set (single shard with 100M tokens)
                n = len(shards) * SHARD_SIZE * 1e-9
            else:
                n = ((len(shards) - 1) * SHARD_SIZE + LAST_SHARD_SIZE) * 1e-9
            print(f'found {len(shards):,} shard(s) for "{split}" split -> ({n:.2f} B tokens)')
        return shards   # return list of full paths to shards

    def _load_shard(self, shard_idx: int):
        """Loads a single shard as a PyTorch tensor of tokens, based on the `shard_idx`."""
        path = self.shards[shard_idx]       # get the full shard path
        if path in self.cache:              # check if shard is already loaded
            return self.cache[path]         # return the cached shard
        self.cache = {}                     # clear the cache (high memory consumption)
        arr = np.load(path)                             # load the shard as a numpy array
        tokens = torch.tensor(arr, dtype=torch.long)    # convert to PyTorch tensor with int64 dtype
        self.cache[path] = tokens                       # cache the loaded shard
        return tokens
    
    def __getitem__(self, idx: int):
        """Returns a single batch of input and target sequences based on `idx` - a batch index."""
        chunk_size = self.batch_size * self.block_size              # chunk size (tokens in one batch)
        global_idx = idx * chunk_size                               # starting position (across all shards)
        
        # determine corresponding shard index from global index:
        shard_idx = global_idx // SHARD_SIZE                                # get index of current shard file by the default shard size of 100M
        if shard_idx == len(self.shards) - 1:                               # if inside the last shard
            # subtract the total tokens in all previous shards (98 shards × 100M) to get the position in the last shard:
            local_idx = global_idx - ((len(self.shards) - 1) * SHARD_SIZE)  # index within the LAST shard
        else:
            local_idx = global_idx % SHARD_SIZE                             # index within all other shards 
        
        tokens = self._load_shard(shard_idx)                    # load the corresponding shard tokens
        if local_idx + chunk_size >= tokens.shape[0]:           # if the current shard will be exhausted
            X1 = tokens[local_idx:]                             # store available tokens of current shard
            y1 = tokens[local_idx + 1:]                         # corresponding target sequence (next token for each sample)
            shard_idx += 1                                      # move to the next shard (no circular indexing as the last shard will never cross boundaries)
            tokens = self._load_shard(shard_idx)                # load the next shard
            rem = chunk_size - X1.shape[0]                      # remaining tokens needed for X to complete the chunk 
            
            if rem == 0:                            # if X was exactly filled but y wasn't
                X = X1                              # X is unchanged 
                y2 = tokens[:1]                     # y is missing one token (due to starting index +1)
                y = torch.cat((y1, y2), dim=0)      # concatenate y tensor
            elif rem > 0:                           # if X and y both need to be filled
                X2 = tokens[: rem]                  # get the remaining tokens from next shard to complete chunk
                y2 = tokens[: rem + 1]              # corresponding target sequence
                X = torch.cat((X1, X2), dim=0)      # concatenate X
                y = torch.cat((y1, y2), dim=0)      # concatenate y  
            else:
                X, y = X1, y1                       # X, y are unchanged (already filled)
        else:       # normal case (no shard boundary crossing)
            X = tokens[local_idx: local_idx + chunk_size]                   # get the input sequence
            y = tokens[local_idx + 1: local_idx + chunk_size + 1]           # get the target sequence (next token for each sample)
        return X.view(self.batch_size, -1), y.view(self.batch_size, -1)     # return with shapes [batch_size, block_size]

    def __len__(self):
        """
        Example calculations for `batch_size=16` and `block_size=1024`:
        ```
        >> idx=598,143 | global_idx=9,799,974,912 | shard_idx=97 | local_idx=99,974,912 | shard 97 size: 100.0M
        >> crossing shard: 97 -> 98 | local_idx=99,991,296 | rem=7,680 | shard 98 size: 82.6M

        >> idx=603,184 | global_idx=9,882,566,656 | shard_idx=98 | local_idx=82,566,656 | shard 98 size: 82.6M
        ```
        This shows that a final `local_idx` of `82,566,656 + 16,284 = 82,583,040` in the last shard was reached,
        resulting in a leftover of `82,590,278 - 82,583,040 = 7,238` tokens. These last remaining `7,238` tokens
        will never be used as they are less than a chunk size of `batch_size * block_size = 16,384` and because
        the total number of batches was calculated to fit within all available tokens using integer division.
        """
        chunk_size = self.batch_size * self.block_size
        if self.split == "val":
            return (len(self.shards) * SHARD_SIZE) // chunk_size   
        # else, for the training set, account for the last shard holding 82,590,278 tokens 
        return ((len(self.shards) - 1) * SHARD_SIZE + LAST_SHARD_SIZE) // chunk_size     


class TinyShakespeare(Dataset):
    """
    PyTorch Dataset for the Tiny Shakespeare dataset, found in `input.txt`.

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


def cycle(iterable):
    """
    Infinitely cycles over an iterable object (e.g. a `DataLoader`) using a generator.
    Used in replacement to `itertools.cycle()` to prevent memory leaks for large datasets like `FineWebEdu()`.
    See: https://github.com/pytorch/pytorch/issues/23900
    """
    iterator = iter(iterable)
    while True:                         
        try:    
            yield next(iterator)        # yield the next item in the iterator
        except StopIteration:           # iterator reaches the end
            iterator = iter(iterable)   # reset the iterator 

if __name__ == "__main__":

    batch_size = 16             # samples per forward pass
    block_size = 1024           # context length

    # ----- DataLoader EXAMPLES with FineWebEdu Sample-10BT ----- #

    train_dataset = FineWebEdu(batch_size, block_size, split="train")
    train_sampler = DistributedSampler(
        train_dataset,
        num_replicas=8,
        rank=0,
        shuffle=False
    )
    train_loader = DataLoader(
        train_dataset,
        batch_size=None,            # must be set to None
        sampler=train_sampler,      # using a DistributedSampler
        pin_memory=True,
        shuffle=False
    )

    print(f"\ntokens per mini-batch: {batch_size * block_size:,} (mini-batch size {batch_size:,})")
    print(f"{len(train_loader):,} available mini-batches per epoch (per GPU)\n")    # per GPU if using DDP
    
    train_iter = cycle(train_loader)

    # example traversal through one epoch of the DataLoader
    n = len(train_loader) * 2
    for i in range(n):
        X, y = next(train_iter)     # get next X, y batch
        progress_str = (
            f"\rbatch: {i + 1:,}/{n:,} | "
            f"{X.shape, y.shape}"
        )
        print(progress_str, end="")

    # ----- DataLoader EXAMPLES with FineWebEdu Sample-10BT ----- #

    # print(f"creating DataLoader for FineWebEdu Sample-10BT dataset..\n")
    # train_loader = DataLoader(
    #     FineWebEdu(batch_size, block_size, split="train"),
    #     batch_size=None,    # must be set to None
    #     shuffle=False,      # iterate through shards sequentially if shuffling=False
    # )

    # print(f"\ntokens per batch: {batch_size * block_size:,} (batch size {batch_size:,})")
    # print(f"{len(train_loader):,} available batches per epoch\n")
    
    # train_iter = iter(train_loader)

    # # example traversal through on epoch of the DataLoader
    # n = len(train_loader)
    # for i in range(n):
    #     X, y = next(train_iter)
    #     progress_str = (
    #         f"\rbatch: {i + 1:,}/{n:,} | "
    #         f"{X.shape, y.shape}"
    #     )
    #     print(progress_str, end="") 

    #-------------------------------------------------------
    # ----- DataLoader examples with TinyShakespeare ----- #

    # chunk_sampling = False      # batch processing method

    # if chunk_sampling:
    #     print(f"\nutilising chunk sampling (non-overlapping batches)")
    #     train_loader = DataLoader(
    #         TinyShakespeare(block_size=1024, batch_size=16),
    #         batch_size=None,    # must be set to None
    #         shuffle=False,
    #     )
    # else:
    #     print(f"\nutilising overlapping samples across batches")
    #     train_loader = DataLoader(
    #         TinyShakespeare(block_size),    # NO specified batch_size within Dataset class
    #         batch_size=batch_size,          # specify DataLoader batch size parameter
    #         shuffle=False
    #     )
    # print(f"\ntokens per batch: {batch_size * block_size:,} (batch size {batch_size:,})")
    # print(f"{len(train_loader):,} available batches per epoch")
    
    # X, y = next(iter(train_loader))
    # print(X.shape, y.shape)             # shape --> [batch_size, block_size]

    # # --- Usage with DistributedSampler:

    # print(f"\nusing DistributedSampler with DataLoader..")
    # train_dataset = TinyShakespeare(        # load custom Dataset class for training
    #     block_size=block_size,
    #     verbose=False
    # )
    # train_sampler = DistributedSampler(     # for DDP: divides the dataset into equal-sized chunks across all GPUs (processes)
    #     train_dataset,
    #     num_replicas=8,                     # total no. of processes (using 8 GPUs as an example)
    #     rank=0,                             # current GPU integer ID (using the first GPU as an example)
    #     shuffle=True
    # )
    # train_loader_wDS = DataLoader(
    #     train_dataset,
    #     batch_size=batch_size,
    #     sampler=train_sampler,              # WITH DistributedSampler
    # )
    # train_loader= DataLoader(               # WITHOUT Distributed
    #     train_dataset,
    #     batch_size=batch_size,
    #     shuffle=True,                       
    # )
    # print("\navailable batches per epoch: ", end="")
    # print(f"{len(train_loader):,} (without) | {len(train_loader_wDS):,} (with)")
