import functools
import torch
from torch import nn
import random
from  torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from models.base_train import run, init_train
from models.base_encoder import Encoder, create_optimizer
from data_loader.data_loader import BaseBatch, BaseDataLoader
from models.base_test import init_test, eval_dataset, reset_unk_weight
from utils.similarity_calculator import Similarity
from utils.constant import DEVICE, RANDOM_SEED
import numpy as np

random_seed = RANDOM_SEED
torch.manual_seed(random_seed)
random.seed(random_seed)
np.random.seed(random_seed)

device = DEVICE

class Batch(BaseBatch):
    def set_src(self, src_tensor, src_lens, src_perm_idx, src_gold_kb_ids):
        self.src_tensor = src_tensor
        self.src_lens = src_lens
        self.src_perm_idx = src_perm_idx
        self.gold_kb_ids = src_gold_kb_ids
        self.src_flag = True

    def set_trg(self, trg_tensor, trg_lens, trg_perm_idx, trg_kb_ids):
        self.trg_tensor = trg_tensor
        self.trg_lens = trg_lens
        self.trg_perm_idx = trg_perm_idx
        self.trg_kb_ids = trg_kb_ids
        self.trg_flag = True

    def set_mid(self, mid_tensor, mid_lens, mid_perm_idx, mid_kb_ids):
        self.mid_tensor = mid_tensor
        self.mid_lens = mid_lens
        self.mid_perm_idx = mid_perm_idx
        self.mid_kb_ids = mid_kb_ids
        self.mid_flag = True

    def to(self, device):
        if self.src_flag:
            self.src_tensor = self.src_tensor.to(device)
            self.src_lens = self.src_lens.to(device)
            self.src_perm_idx = self.src_perm_idx.to(device)
        if self.trg_flag:
            self.trg_tensor = self.trg_tensor.to(device)
            self.trg_lens = self.trg_lens.to(device)
            self.trg_perm_idx = self.trg_perm_idx.to(device)
        if self.mid_flag:
            self.mid_tensor = self.mid_tensor.to(device)
            self.mid_lens = self.mid_lens.to(device)
            self.mid_perm_idx = self.mid_perm_idx.to(device)

    def get_all(self):
        return  self.src_tensor, self.src_lens, self.src_perm_idx, \
                self.trg_tensor, self.trg_lens, self.trg_perm_idx
    def get_src(self):
        return self.src_tensor, self.src_lens, self.src_perm_idx

    def get_trg(self):
        return self.trg_tensor, self.trg_lens, self.trg_perm_idx

    def get_mid(self):
        return self.mid_tensor, self.mid_lens, self.mid_perm_idx




class DataLoader(BaseDataLoader):
    def __init__(self, is_train, args, train_file, dev_file, test_file):
        super(DataLoader,self).__init__(is_train=is_train, args=args, train_file=train_file, dev_file=dev_file, test_file=test_file)

    def new_batch(self):
        return Batch()

    def load_all_data(self, file_name, str_idx, id_idx, x2i_map, freq_map, encoding_num, type_idx):
        line_tot = 0
        with open(file_name, "r", encoding="utf-8") as fin:
            for line in fin:
                line_tot += 1
                tks = line.strip().split(" ||| ")
                if encoding_num == 1:
                    # make it a list
                    string = [x2i_map[char] for char in tks[str_idx]]
                    all_string = [string]
                else:
                    all_string = []
                    alias = self.get_alias(tks, str_idx, id_idx, encoding_num)
                    for i in range(encoding_num):
                        string = [x2i_map[char] for char in alias[i]]
                        all_string.append(string)

                for s in all_string:
                    for ss in s:
                        freq_map[ss] += 1

                yield ([all_string], tks[id_idx])
        print("[INFO] number of lines in {}: {}".format(file_name, str(line_tot)))

    def transform_one_batch(self, batch_data: list) -> list:
        batch_size = len(batch_data)
        batch_lens = torch.LongTensor([len(x) for x in batch_data])
        max_len = torch.max(batch_lens)
        batch_tensor = torch.zeros((batch_size, max_len)).long()
        for idx, (seq, seq_len) in enumerate(zip(batch_data, batch_lens)):
            batch_tensor[idx, :seq_len] = torch.LongTensor(seq)
        # sort
        batch_lens, perm_idx = torch.sort(batch_lens, dim=0, descending=True)
        batch_tensor = batch_tensor[perm_idx]
        # [b, max_len] - > [max_len, b]
        batch_tensor = torch.transpose(batch_tensor, 1, 0)

        # perm idx is used to recover the original order, as src and trg will be different!
        return [batch_tensor, batch_lens, perm_idx]


class LSTMEncoder(Encoder):
    def __init__(self, src_vocab_size, trg_vocab_size, embed_size, hidden_size, similarity_measure:Similarity,
                 use_mid, use_avg, mid_vocab_size=0):
        super(LSTMEncoder, self).__init__(hidden_size)
        self.name = "bilstm"
        self.src_vocab_size = src_vocab_size
        self.trg_vocab_size = trg_vocab_size
        self.embed_size = embed_size
        self.hidden_size = hidden_size
        self.use_mid = use_mid
        self.mid_vocab_size=mid_vocab_size
        self.src_lookup = nn.Embedding(src_vocab_size, embed_size)
        self.trg_lookup = nn.Embedding(trg_vocab_size, embed_size)
        torch.nn.init.xavier_uniform_(self.src_lookup.weight, gain=1)
        torch.nn.init.xavier_uniform_(self.trg_lookup.weight, gain=1)

        self.src_lstm = nn.LSTM(embed_size, int(hidden_size / 2), bidirectional=True)
        self.trg_lstm = nn.LSTM(embed_size, int(hidden_size / 2), bidirectional=True)

        self.reset_lstm_parameters(self.src_lstm)
        self.reset_lstm_parameters(self.trg_lstm)

        if use_mid:
            self.mid_lookup = nn.Embedding(mid_vocab_size, embed_size)
            self.mid_lstm = nn.LSTM(embed_size, int(hidden_size / 2), bidirectional=True)
            torch.nn.init.xavier_uniform_(self.mid_lookup.weight, gain=1)

        self.similarity_measure = similarity_measure
        self.use_avg = use_avg

    def reset_lstm_parameters(self, lstm):
        for name, param in lstm.state_dict().items():
            if "weight" in name:
                nn.init.xavier_uniform_(param)
            elif "bias" in name:
                if "bias_hh" in name:
                    p = torch.zeros_like(param)
                    p[512:1024] = 1.0
                    param.copy_(p)
                else:
                    nn.init.constant_(param, 0.0)

    # calc_batch_similarity will return the similarity of the batch
    # while calc encode only return the encoding result of src or trg of the batch
    def calc_encode(self, batch, is_src, is_mid=False):
        # input: [len, batch] or [len, batch, pp_vec_size]
        # embed: [len, batch, embed_size]
        if is_mid:
            lookup = self.mid_lookup
            lstm = self.mid_lstm
            input, input_lens, perm_idx = batch.get_mid()
        else:
            if is_src:
                lookup = self.src_lookup
                lstm = self.src_lstm
                input, input_lens, perm_idx = batch.get_src()
            else:
                lookup = self.trg_lookup
                lstm = self.trg_lstm
                input, input_lens, perm_idx = batch.get_trg()


        embeds = lookup(input)
        packed = pack_padded_sequence(embeds, input_lens, batch_first=False)
        packed_output, (hidden, cached) = lstm(packed)
        if not self.use_avg:
            # get the last hidden state
            # [2, batch, hidden]
            encoded = hidden
            # [batch, 2, hidden]
            encoded = torch.transpose(encoded, 0, 1).contiguous()
            # combine hidden state of two directions
            # [batch, hidden * 2]
            bi_encoded = encoded.view(-1, self.hidden_size)
            reorder_encoded = bi_encoded[torch.sort(perm_idx, 0)[1]]
        else:
            output, _ = pad_packed_sequence(packed_output)

            # [batch, len, 2 * hidden]
            encoded = torch.transpose(output, 0, 1)
            # [batch, 2 * hidden]
            avg_encoded = torch.sum(encoded, dim=1) / input_lens.unsqueeze(-1).float()
            reorder_encoded = avg_encoded[torch.sort(perm_idx, 0)[1]]

        return reorder_encoded

class AvgLSTMEncoder(LSTMEncoder):
    def __init__(self, src_vocab_size, trg_vocab_size, embed_size, hidden_size, similarity_measure:Similarity,
                 use_mid, mid_vocab_size=0):
        super(AvgLSTMEncoder, self).__init__(src_vocab_size, trg_vocab_size, embed_size, hidden_size, similarity_measure,
                 use_mid, mid_vocab_size)


    # calc_batch_similarity will return the similarity of the batch
    # while calc encode only return the encoding result of src or trg of the batch
    def calc_encode(self, batch, is_src, is_mid=False):
        # input: [len, batch] or [len, batch, pp_vec_size]
        # embed: [len, batch, embed_size]
        if is_mid:
            lookup = self.mid_lookup
            lstm = self.mid_lstm
            input, input_lens, perm_idx = batch.get_mid()
        else:
            if is_src:
                lookup = self.src_lookup
                lstm = self.src_lstm
                input, input_lens, perm_idx = batch.get_src()
            else:
                lookup = self.trg_lookup
                lstm = self.trg_lstm
                input, input_lens, perm_idx = batch.get_trg()

        embeds = lookup(input)
        packed = pack_padded_sequence(embeds, input_lens, batch_first=False)
        packed_output, (hidden, cached) = lstm(packed)
        output, _ = pad_packed_sequence(packed_output)

        # [batch, len, 2 * hidden]
        encoded = torch.transpose(output, 0, 1)
        # [batch, 2 * hidden]
        avg_encoded = torch.sum(encoded, dim=1) / input_lens.unsqueeze(-1).float()
        reorder_encoded = avg_encoded[torch.sort(perm_idx, 0)[1]]

        return reorder_encoded

def save_model(model:LSTMEncoder, epoch, loss, optimizer, model_path):
    torch.save({"model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "src_vocab_size": model.src_vocab_size,
                "trg_vocab_size": model.trg_vocab_size,
                "mid_vocab_size": model.mid_vocab_size,
                "embed_size": model.embed_size,
                "hidden_size": model.hidden_size,
                "similarity_measure": model.similarity_measure.method,
                "epoch": epoch,
                "loss": loss}, model_path)
    print("[INFO] save model!")

def main(args):
    use_avg = "avg" in args.model

    if args.is_train:
        data_loader, criterion, similarity_measure = init_train(args, DataLoader)
        model = LSTMEncoder(data_loader.src_vocab_size, data_loader.trg_vocab_size,
                    args.embed_size, args.hidden_size,
                    similarity_measure,
                    args.use_mid, use_avg, data_loader.mid_vocab_size)
        optimizer, scheduler = create_optimizer(args.trainer, args.learning_rate, model, args.lr_decay)
        if args.resume:
            model_info = torch.load(args.model_path + "_" + str(args.test_epoch) + ".tar")
            model.load_state_dict(model_info["model_state_dict"])
            optimizer.load_state_dict(model_info["optimizer_state_dict"])
            print("[INFO] load model from epoch {:d} train loss: {:.4f}".format(model_info["epoch"], model_info["loss"]))

        model.set_similarity_matrix()
        run(data_loader, model, criterion, optimizer, scheduler, similarity_measure, save_model, args)
    else:
        base_data_loader, intermedia_stuff = init_test(args, DataLoader)
        model_info = torch.load(args.model_path + "_" + str(args.test_epoch) + ".tar")
        similarity_measure = Similarity(args.similarity_measure)
        model = LSTMEncoder(model_info["src_vocab_size"], model_info["trg_vocab_size"],
                        args.embed_size, args.hidden_size,
                        similarity_measure=similarity_measure,
                        use_mid=args.use_mid, use_avg=use_avg,
                        mid_vocab_size=model_info.get("mid_vocab_size", 0))

        model.load_state_dict(model_info["model_state_dict"], strict=False)
        reset_unk_weight(model)
        model.set_similarity_matrix()
        eval_dataset(model, similarity_measure, base_data_loader, args.encoded_test_file, args.load_encoded_test,
                     args.encoded_kb_file, args.load_encoded_kb, intermedia_stuff, args.method, args.trg_encoding_num,
                     args.mid_encoding_num, args.result_file, args.record_recall)
