import sys
sys.path.append("/home/shuyanzh/workshop/cmu_lorelei_edl/")
from collections import defaultdict
import functools
import torch
from torch import nn
from torch import optim
import random
import time
import numpy as np
import pickle
from mention_matching.pbel.similarity_calculator import Similarity
import argparse
from typing import List, Generator
from mention_matching.pbel.criterion import NSHingeLoss, MultiMarginLoss, CrossEntropyLoss
from collections import defaultdict
from itertools import combinations

print = functools.partial(print, flush=True)
PATIENT = 50
EPOCH_CHECK = 2
PP_VEC_SIZE = 22
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class BaseBatch:
    def __init__(self):
        self.src_flag=False
        self.trg_flag=False
        self.mega_flag=False
        self.negative_num=0
        self.src_gold_kb_ids = None
        self.trg_kb_ids = None

    def set_src(self, *args, **kwargs):
        pass
    def set_trg(self, *args, **kwargs):
        pass
    def set_mega(self, *args, **kwargs):
        pass
    def get_all(self, *args, **kwargs):
        pass
    def get_src(self,  *args, **kwargs):
        pass
    def get_trg(self,  *args, **kwargs):
        pass
    def get_mega(self,  *args, **kwargs):
        pass
    def to(self,  *args, **kwargs):
        pass


class FileInfo:
    def __init__(self):
        self.src_file_name = None
        self.src_str_idx = None
        self.src_id_idx = None
        self.trg_file_name = None
        self.trg_str_idx = None
        self.trg_id_idx = None

    def set_all(self, file_name, src_str_idx, trg_str_idx, id_idx):
        self.src_file_name = file_name
        self.trg_file_name = file_name
        self.src_str_idx = int(src_str_idx)
        self.trg_str_idx = int(trg_str_idx)
        self.src_id_idx = int(id_idx)
        self.trg_id_idx = int(id_idx)

    def set_src(self, file_name, str_idx, id_idx):
        self.src_file_name = file_name
        self.src_str_idx = int(str_idx)
        self.src_id_idx = int(id_idx)

    def set_trg(self, file_name, str_idx, id_idx):
        self.trg_file_name = file_name
        self.trg_str_idx = int(str_idx)
        self.trg_id_idx = int(id_idx)


class Encoder(nn.Module):
    def __init__(self):
        super(Encoder, self).__init__()

    def calc_batch_similarity(self, batch:BaseBatch, use_negative=False):
        src_encoded = self.calc_encode(batch, is_src=True)
        #[batch_size, hidden_state]
        trg_encoded = self.calc_encode(batch, is_src=False)
        if batch.mega_flag:
            mega_encoded = self.calc_encode(batch, is_src=False, is_mega=True)
            #[batch_size * 2, hidden state ]
            trg_encoded = torch.cat((trg_encoded, mega_encoded), dim=0)
        if use_negative:
            ns = batch.negative_num
        else:
            ns = None
        # [batch_size, negative_sample_size]
        similarity = self.similarity_measure(src_encoded, trg_encoded, self.bilinear, split=False, pieces=0, negative_sample=ns)
        return similarity

    def calc_encode(self, *args, **kwargs)->torch.Tensor:
        pass


class BaseDataLoader:
        def __init__(self, is_train, map_file, batch_size, mega_size, use_panphon, pad_str, train_file:FileInfo, dev_file:FileInfo, test_file:FileInfo):
            self.batch_size = batch_size
            self.train_file = train_file
            self.dev_file = dev_file
            self.use_panphon = use_panphon
            self.map_file = map_file
            self.pad_str = pad_str
            self.pad_idx = 0
            self.train_file = train_file
            self.dev_file = dev_file
            self.test_file = test_file
            self.mega_batch_size = mega_size * batch_size
            if is_train:
                self.init_train()
            else:
                self.init_test()

        def init_train(self):
            self.x2i_src = defaultdict(lambda: len(self.x2i_src))
            self.x2i_trg = defaultdict(lambda: len(self.x2i_trg))
            # make sure pad is 0
            self.x2i_src[self.pad_str]
            self.x2i_trg[self.pad_str]
            self.train_src = list(self.load_data(self.train_file.src_file_name, self.train_file.src_str_idx, self.train_file.src_id_idx, is_src=True))
            self.train_trg = list(self.load_data(self.train_file.trg_file_name, self.train_file.trg_str_idx, self.train_file.trg_id_idx, is_src=False))
            self.non_neg_mask = self.get_non_negative_mask()
            # save map
            self.save_map(self.x2i_src, self.map_file + "_src.pkl")
            self.save_map(self.x2i_trg, self.map_file + "_trg.pkl")

            # sort training data by input length
            self.src_vocab_size = len(self.x2i_src)
            self.trg_vocab_size = len(self.x2i_trg)
            self.x2i_src = defaultdict(lambda: self.x2i_src[self.pad_str], self.x2i_src)
            self.x2i_trg = defaultdict(lambda: self.x2i_trg[self.pad_str], self.x2i_trg)

            if self.dev_file:
                self.dev_src = list(self.load_data(self.dev_file.src_file_name, self.dev_file.src_str_idx, self.dev_file.src_id_idx, is_src=True))
                self.dev_trg = list(self.load_data(self.dev_file.trg_file_name, self.dev_file.trg_str_idx, self.dev_file.trg_id_idx, is_src=False))
                n = min(len(self.dev_src), 2000)
                self.dev_src, self.dev_trg = self.dev_src[:n], self.dev_trg[:n]
            else:
                self.dev_src, self.dev_trg = None, None

        def get_non_negative_mask(self):
            id_idx_map = defaultdict(list)
            for idx, (_, kb_id) in enumerate(self.train_src):
                id_idx_map[kb_id].append(idx)
            mask = torch.zeros((len(self.train_src), len(self.train_src)))
            for _, idx_list in id_idx_map.items():
                idx_pairs = combinations(idx_list, 2)
                for i, j in idx_pairs:
                    mask[i,j] = 1
                    mask[j,i] = 1
            mask += torch.eye(len(self.train_src), len(self.train_src))
            mask = mask.long()
            # mask = mask.to(device)
            return mask

        def init_test(self):
            self.x2i_src = self.load_map(self.map_file + "_src.pkl")
            self.x2i_trg = self.load_map(self.map_file + "_trg.pkl")
            self.i2c_src = {v: k for k, v in self.x2i_src.items()}
            self.i2c_trg = {v: k for k, v in self.x2i_trg.items()}
            if self.test_file.src_file_name is not None:
                self.test_src = list(self.load_data(self.test_file.src_file_name, self.test_file.src_str_idx, self.test_file.src_id_idx, is_src=True))
            if self.test_file.trg_file_name is not None:
                self.test_trg = list(self.load_data(self.test_file.trg_file_name, self.test_file.trg_str_idx, self.test_file.trg_id_idx, is_src=False))

        def load_data(self, *args, **kwargs):
            pass

        def transform_one_batch(self, *args, **kwargs) -> list:
            pass

        def new_batch(self) -> BaseBatch:
            pass

        # data from one side
        def prepare_batch(self, side_data, data_idx):
            words = [side_data[idx][0] for idx in data_idx]
            kb_ids = [side_data[idx][1] for idx in data_idx]
            batch_info = self.transform_one_batch(words)

            return batch_info, kb_ids

        def create_batch(self, dataset, data_src=None, data_trg=None, data_mega=None) -> List[BaseBatch]:
            batches = []
            data_idx = [i for i in range(len(data_src))] if data_src is not None else [i for i in range(len(data_trg))]
            if dataset == "train":
                random.shuffle(data_idx)
            for i in range(0, len(data_idx), self.batch_size):
                batch = self.new_batch()
                cur_size = min(self.batch_size, len(data_idx) - i)
                cur_data_idx = data_idx[i:i + cur_size]
                if data_src is not None:
                    batch_info, src_gold_kb_ids = self.prepare_batch(data_src, cur_data_idx)
                    batch.set_src(*batch_info, src_gold_kb_ids)
                if data_trg is not None:
                    batch_info, trg_kb_ids = self.prepare_batch(data_trg, cur_data_idx)
                    batch.set_trg(*batch_info, trg_kb_ids)
                if data_mega is not None:
                    batch_info, trg_kb_ids = self.prepare_batch(data_mega, cur_data_idx)
                    batch.set_mega(*batch_info, trg_kb_ids)
                # move to device
                batch.to(device)
                batches.append(batch)

            return batches

        # pad both source and target words
        def create_batches(self, dataset:str, is_src=None) -> List[BaseBatch]:
            if dataset == "train":
                batches = self.create_batch(dataset, self.train_src, self.train_trg)
            elif dataset == "dev":
                batches = self.create_batch(dataset, self.dev_src, self.dev_trg)
            else:
                assert is_src is not None
                if is_src:
                    batches = self.create_batch(dataset, self.test_src, None)
                else:
                    batches = self.create_batch(dataset, None, self.test_trg)
            return batches

        def create_megabatch(self, model:Encoder):
            # only for training
            data_src, data_trg = self.train_src, self.train_trg
            data_idx = [i for i in range(len(data_src))]
            random.shuffle(data_idx)
            for i in range(0, len(data_idx), self.mega_batch_size):
                batch = self.new_batch()
                cur_size = min(self.mega_batch_size, len(data_idx) - i)
                cur_data_idx = data_idx[i: i+cur_size]
                # src
                batch_info, src_gold_kb_ids = self.prepare_batch(data_src, cur_data_idx)
                batch.set_src(*batch_info, src_gold_kb_ids)
                # trg
                batch_info, trg_kb_ids = self.prepare_batch(data_trg, cur_data_idx)
                batch.set_trg(*batch_info, trg_kb_ids)

                with torch.no_grad():
                    model.eval()
                    batch.to(device)
                    M = model.calc_batch_similarity(batch, use_negative=False)
                    model.train()

                negative_num = min(1, cur_size - 1)

                # mask the non negative samples and the diagonal
                idx_tensor = torch.LongTensor(cur_data_idx)
                non_neg_mask = torch.index_select(self.non_neg_mask, 0, idx_tensor)
                non_neg_mask = torch.index_select(non_neg_mask, 1, idx_tensor)
                non_neg_mask = non_neg_mask.to(device)
                masked_M = M.masked_fill(non_neg_mask == 1, -1e9)

                # negative_idx = [batch_size, 1]
                _, negative_idx = torch.topk(masked_M, k=negative_num, dim=-1)
                # negative sample
                cur_negative_idx = [cur_data_idx[idx.item()] for idx in negative_idx]
                mega_src = [data_src[idx] for idx in cur_data_idx]
                mega_trg = [data_trg[idx] for idx in cur_data_idx]
                mega_negative = [data_trg[idx] for idx in cur_negative_idx]

                cur_mega_batch = self.create_batch("train", mega_src, mega_trg, mega_negative)
                for b in cur_mega_batch:
                    yield b


        def save_map(self, map, map_file):
            # save map
            with open(map_file, "wb") as f:
                pickle.dump(dict(map), f)
                print("[INFO] save x to idx map to :{}, len: {:d}".format(map_file + "_src.pkl", len(map)))

        def load_map(self, map_file):
            with open(map_file, "rb") as f:
                m = pickle.load(f)
                m = defaultdict(lambda: m[self.pad_str], m)
                print("[INFO] load x to idx map from {}, len: {:d}".format(map_file, len(m)))
                return m

def list2nparr(org_list:List[np.ndarray], hidden_size:int):
    # last batch might not match the size
    encodings = np.array(org_list[:-1])
    encodings = np.reshape(encodings, (-1, hidden_size))
    encodings = np.append(encodings, np.array(org_list[-1]), axis=0)

    return encodings

def calc_batch_loss(model, criterion, batch: BaseBatch):
    # src_tensor, src_lens, src_perm_idx, trg_tensor, trg_kb_id, trg_lens, trg_perm_idx
    similarity = model.calc_batch_similarity(batch, use_negative=True)
    loss = criterion(similarity)
    return loss

def get_unique_kb_idx(kb_id_list: list):
    find_kb_ids = []
    unique_kb_idx = []
    for i, id in enumerate(kb_id_list):
        if id in find_kb_ids:
            continue
        else:
            unique_kb_idx.append(i)
            find_kb_ids.append(id)
    return np.array(unique_kb_idx)

# evaluate the whole dataset
def eval_data(model: Encoder, train_batches:List[BaseBatch], dev_batches: List[BaseBatch], similarity_measure: Similarity, topk=30):
    # treat train target strings as the KB
    recall = 0
    tot = 0
    KB_encodings = []
    KB_ids = []
    for batch in train_batches:
        KB_encodings.append(np.array(model.calc_encode(batch, is_src=False).cpu()))
        KB_ids += batch.trg_kb_ids
    assert len(KB_encodings) == len(train_batches)

    src_encodings = []
    trg_encodings = []
    trg_kb_ids = []
    for batch in dev_batches:
        src_encodings.append(np.array(model.calc_encode(batch, is_src=True).cpu()))
        trg_encodings.append(np.array(model.calc_encode(batch, is_src=False).cpu()))
        trg_kb_ids += batch.trg_kb_ids
    assert len(src_encodings) == len(dev_batches)
    assert len(trg_encodings) == len(dev_batches)

    src_encodings = list2nparr(src_encodings, model.hidden_size)
    trg_encodings = list2nparr(trg_encodings, model.hidden_size)
    KB_encodings = list2nparr(KB_encodings, model.hidden_size)
    # prune KB_encodings so that all entities are unique
    unique_kb_idx = get_unique_kb_idx(KB_ids)
    KB_encodings = KB_encodings[unique_kb_idx]

    all_trg_encodings = np.append(trg_encodings, KB_encodings, axis=0)
    n = max(all_trg_encodings.shape[0], 80000)
    all_trg_encodings = all_trg_encodings[:n]
    # calculate similarity`
    # [dev_size, dev_size + kb_size]
    scores = similarity_measure(src_encodings, all_trg_encodings, model.bilinear, split=True, pieces=10, negative_sample=None)
    for entry_idx, entry_scores in enumerate(scores):
        ranked_idxes = entry_scores.argsort()[::-1]
        # the correct index is entry_idx
        if entry_idx in ranked_idxes[:topk]:
            recall += 1
        tot += 1

    return recall, tot


def run(data_loader: BaseDataLoader, encoder: Encoder, criterion, optimizer: optim,
          similarity_measure: Similarity, save_model,
          args:argparse.Namespace):
    encoder.to(device)
    best_acc = float('-inf')
    last_update = 0
    for ep in range(args.max_epoch):
        encoder.train()
        train_loss = 0.0
        start_time = time.time()
        if not args.mega:
            train_batches = data_loader.create_batches("train")
        else:
            if ep <= 30:
                train_batches = data_loader.create_batches("train")
            else:
                train_batches = data_loader.create_megabatch(encoder)
        batch_num = 0
        for idx, batch in enumerate(train_batches):
            optimizer.zero_grad()
            cur_loss = calc_batch_loss(encoder, criterion, batch)
            train_loss += cur_loss.item()
            cur_loss.backward()
            optimizer.step()
            batch_num += 1
        print("[INFO] epoch {:d}: train loss={:.4f}, time={:.2f}".format(ep, train_loss / batch_num,
                                                                         time.time()-start_time))

        if (ep + 1) % EPOCH_CHECK == 0:
            with torch.no_grad():
                encoder.eval()
                # eval
                train_batches = data_loader.create_batches("train")
                dev_batches = data_loader.create_batches("dev")
                start_time = time.time()
                recall, tot = eval_data(encoder, train_batches, dev_batches, similarity_measure)
                dev_acc = recall / float(tot)
                if dev_acc > best_acc:
                    best_acc = dev_acc
                    last_update = ep + 1
                    save_model(encoder, optimizer, args.model_path + "_" + "best" + ".tar")
                save_model(encoder, optimizer, args.model_path + "_" + "last" + ".tar")
                print("[INFO] epoch {:d}: dev acc={:.1f}/{:.1f}={:.4f}, time={:.2f}".format(ep, recall, tot, dev_acc,
                                                                                            time.time()-start_time))
                if ep + 1 - last_update > PATIENT:
                    break


def init_train(args, DataLoader):
    train_file = FileInfo()
    train_file.set_all(args.train_file, args.src_idx, args.trg_idx, args.trg_id_idx)
    dev_file = FileInfo()
    dev_file.set_all(args.dev_file, args.src_idx, args.trg_idx, args.trg_id_idx)
    data_loader = DataLoader(True, args.map_file, args.batch_size, args.mega_size, args.use_panphon, train_file=train_file,
                             dev_file=dev_file)
    similarity_measure = Similarity(args.similarity_measure)

    if args.objective == "hinge":
        criterion = MultiMarginLoss(device, margin=args.margin, reduction="mean")
    elif args.objective == "mle":
        criterion = CrossEntropyLoss(reduction="mean")
    else:
        raise NotImplementedError

    return data_loader, criterion, similarity_measure

def create_optimizer(trainer, lr, model):
    if trainer == "adam":
        optimizer = optim.Adam(model.parameters(), lr)
    elif trainer == "sgd":
        optimizer = optim.SGD(model.parameters(), lr)
    else:
        raise NotImplementedError

    return optimizer