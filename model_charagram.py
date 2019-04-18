import sys
sys.path.append("/home/shuyanzh/workshop/cmu_lorelei_edl/")
from collections import defaultdict
import functools
import torch
from torch import nn
from torch import optim
import random
import panphon as pp
from  torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import pickle
from mention_matching.pbel.base_train import FileInfo, BaseBatch, BaseDataLoader, Encoder, init_train, create_optimizer, run
from mention_matching.pbel.base_test import init_test, eval_dataset
from mention_matching.pbel.config import argps
from mention_matching.pbel.similarity_calculator import Similarity

print = functools.partial(print, flush=True)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
START_SYMBOL = "*"
END_SYMBOL = "@"

def get_ngram(string, ngram_list=(2, 3, 4)):
    string = START_SYMBOL + string + END_SYMBOL
    all_ngrams = []
    for n in ngram_list:
        cur_ngram = zip(*[string[i:] for i in range(n)])
        cur_ngram = ["".join(x) for x in cur_ngram]
        all_ngrams += cur_ngram
    return all_ngrams


class Batch(BaseBatch):
    def set_src(self, src_tensor, src_mask, src_gold_kb_ids):
        self.src_tensor = src_tensor.long()
        self.src_mask = src_mask
        self.gold_kb_ids = src_gold_kb_ids
        self.src_flag = True

    def set_trg(self, trg_tensor, trg_mask, trg_kb_ids):
        self.trg_tensor = trg_tensor.long()
        self.trg_mask = trg_mask
        self.trg_kb_ids = trg_kb_ids
        self.trg_flag = True

    def set_mega(self, mega_tensor, mega_mask, mega_trg_kb_ids):
        self.mega_tensor = mega_tensor.long()
        self.mega_mask = mega_mask
        self.mega_trg_kb_ids = mega_trg_kb_ids
        self.mega_flag = True
        self.negative_num = 1

    def to(self, device):
        if self.src_flag:
            self.src_tensor = self.src_tensor.to(device)
            self.src_mask = self.src_mask.to(device)
        if self.trg_flag:
            self.trg_tensor = self.trg_tensor.to(device)
            self.trg_mask = self.trg_mask.to(device)
        if self.mega_flag:
            self.mega_tensor = self.mega_tensor.to(device)
            self.mega_mask = self.mega_mask.to(device)

    def get_all(self):
        return  self.src_tensor, self.src_mask, \
                self.trg_tensor, self.trg_mask

    def get_src(self):
        return self.src_tensor, self.src_mask

    def get_trg(self):
        return self.trg_tensor, self.trg_mask

    def get_mega(self):
        return self.mega_tensor, self.mega_mask


class DataLoader(BaseDataLoader):
    def __init__(self, is_train, map_file, batch_size, mega_size, use_panphon, train_file=None, dev_file=None, test_file=None):
        super(DataLoader,self).__init__(is_train, map_file, batch_size, mega_size, use_panphon, "<pad>", train_file, dev_file, test_file)

    def new_batch(self):
        return Batch()

    def load_data(self, file_name, str_idx, id_idx, is_src):
        if is_src:
            x2i_map = self.x2i_src
        else:
            x2i_map = self.x2i_trg
        line_tot = 0
        with open(file_name, "r", encoding="utf-8") as fin:
            for line in fin:
                line_tot += 1
                tks = line.strip().split(" ||| ")
                string = [x2i_map[ngram] for ngram in get_ngram(tks[str_idx])]
                yield (string, tks[id_idx])
        print("[INFO] number of lines in {}: {}".format(file_name, str(line_tot)))


    def transform_one_batch(self, data):
        data_len = [len(x) for x in data]
        cur_size = len(data_len)
        max_data_len = max(data_len)
        data_tensor = torch.zeros((cur_size, max_data_len))
        for idx, id_list in enumerate(data):
            data_tensor[idx, :data_len[idx]] = torch.LongTensor(id_list)
        mask = (data_tensor != self.pad_idx).unsqueeze(-1)
        return [data_tensor, mask]
    
class Charagram(Encoder):
    def __init__(self, src_vocab_size, trg_vocab_size, embed_size, similarity_measure):
        super(Charagram, self).__init__()
        self.src_vocab_size = src_vocab_size
        self.trg_vocab_size = trg_vocab_size
        self.hidden_size = embed_size
        self.embed_size = embed_size
        # parameters
        self.src_lookup = nn.Embedding(src_vocab_size, embed_size)
        self.trg_lookup = nn.Embedding(trg_vocab_size, embed_size)
        self.bias = nn.Parameter(torch.zeros(1, embed_size), requires_grad=True)

        self.activate = torch.tanh
        self.similarity_measure = similarity_measure
        self.bilinear = nn.Parameter(torch.zeros((self.embed_size, self.embed_size)))

        torch.nn.init.xavier_uniform_(self.src_lookup.weight, gain=1)
        torch.nn.init.xavier_uniform_(self.trg_lookup.weight, gain=1)
        torch.nn.init.xavier_uniform_(self.bias, gain=1)
        torch.nn.init.xavier_uniform_(self.bilinear, gain=1)

    # calc_batch_similarity will return the similarity of the batch
    # while calc encode only return the encoding result of src or trg of the batch
    def calc_encode(self, batch: Batch, is_src, is_mega=False):
        # input: [len, batch] or [len, batch, pp_vec_size]
        # embed: [len, batch, embed_size]
        if is_src:
            lookup = self.src_lookup
            input, mask = batch.get_src()
        else:
            lookup = self.trg_lookup
            input, mask = batch.get_trg()

        if is_mega:
            input, mask = batch.get_mega()
        # [batch_size, max_len, embed_size]
        embed = lookup(input)
        # mask padding
        embed = embed.masked_fill(mask==0, 0)
        # [batch_size, embed_size]
        encoded = self.activate(torch.sum(embed, dim=1, keepdim=False) + self.bias)
        return encoded

def save_model(model:Charagram, optimizer, model_path):
    torch.save({"model_state_dict": model.state_dict(),
                "optimizer_statte_dict": optimizer.state_dict(),
                "src_vocab_size": model.src_vocab_size,
                "trg_vocab_size": model.trg_vocab_size,
                "embed_size": model.embed_size,
                "similarity_measure": model.similarity_measure.method}, model_path)
    print("[INFO] save model!")

if __name__ == "__main__":
    args = argps()
    if args.is_train:
        data_loader, criterion, similarity_measure = init_train(args, DataLoader)
        model = Charagram(data_loader.src_vocab_size, data_loader.trg_vocab_size,
                    args.embed_size, similarity_measure)
        optimizer = create_optimizer(args.trainer, args.learning_rate, model)
        run(data_loader, model, criterion, optimizer, similarity_measure, save_model, args)
    else:
        base_data_loader, intermedia_stuff = init_test(args, DataLoader)
        model_info = torch.load(args.model_path + "_" + str(args.test_epoch) + ".tar")
        similarity_measure = Similarity(args.similarity_measure)
        model = Charagram(model_info["src_vocab_size"], model_info["trg_vocab_size"],
                        model_info["embed_size"],
                        similarity_measure=similarity_measure)

        model.load_state_dict(model_info["model_state_dict"])
        eval_dataset(model, similarity_measure, base_data_loader, args.encoded_test_file, args.load_encoded_test,
                     args.encoded_kb_file, args.load_encoded_kb, intermedia_stuff, args.method, args.result_file, args.record_recall)

