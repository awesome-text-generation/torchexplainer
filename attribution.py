import os.path 
import argparse
import pickle
from tqdm import tqdm 
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.utils.data
from torch import nn
from torch.autograd import Variable, grad
from train import prepare_dataloaders
from dataset import collate_fn, TranslationDataset
from preprocess import read_instances_from_file, convert_instance_to_idx_seq
from transformer.Models import Transformer
from transformer.Translator import Translator

# The first challenge is to handle beam search, this of course means for seq2seq problems we
# apply IG in train phase 
# Another challenge is the encoder decoder problem, i think can be solve by chain rule
# Chain rule 
# ddecoder_output/dinput = ddecoder_output/dencoder_output * dencoder_output/dinput
# model output.backward() -> find gradient with respect to input 
# I don't know why but model() immediately triggers the foward method, 
# pytorch weirdness 1
# class -> object -> object(forward pass params) -> generates forward pass
class Attribution(object):
    ''' Load with trained model and handle the beam search '''

    def __init__(self,opt):
        #opt is from argprass 
        self.opt = opt
        self.device = torch.device('cuda' if opt.cuda else 'cpu')
        self.m = opt.m 
        #opt.model is the model path 
        checkpoint = torch.load(opt.model)
        #model_opt is the model hyper params
        model_opt = checkpoint['settings']
        self.model_opt = model_opt

        model = Transformer(
            model_opt.src_vocab_size,
            model_opt.tgt_vocab_size,
            model_opt.max_token_seq_len,
            tgt_emb_prj_weight_sharing=model_opt.proj_share_weight,
            emb_src_tgt_weight_sharing=model_opt.embs_share_weight,
            d_k=model_opt.d_k,
            d_v=model_opt.d_v,
            d_model=model_opt.d_model,
            d_word_vec=model_opt.d_word_vec,
            d_inner=model_opt.d_inner_hid,
            n_layers=model_opt.n_layers,
            n_head=model_opt.n_head,
            dropout=model_opt.dropout,
            return_attns=opt.return_attns)

        #Load the actual model weights 
        model.load_state_dict(checkpoint['model'])
        print('[Info] Trained model state loaded.')

        model.word_prob_prj = nn.LogSoftmax(dim=1)

        model = model.to(self.device)

        self.model = model
        self.model.eval()

    def attribute_batch(self,training_data,dev=False,debug=False):

        # LongTensor cannot be backpropogated
        def f(x):
            x.to(self.device)
            return x
        ''' Attribute in one batch '''
        #-- Encode
        # Understand the correctness of the code
        # Understand the output -> Visualisation 
        F = []
        for batch in tqdm(training_data, mininterval=2,
            desc='  - (Attributing)   ', leave=False):
            src_seq, src_pos, tgt_seq, tgt_pos = map(f, batch)
            IG = []
            IG ,tgt_IG = [],[]
            for k in range(1,self.m+1):

                pred = self.model(src_seq, src_pos, tgt_seq, tgt_pos,alpha=k/self.m)
                
                val,translated_sentence = torch.max(pred,1)
                tgt_trans_sent = tgt_seq[0][:len(val)]
                if debug:
                    tgt_val = []
                    for pos,ids in enumerate(tgt_trans_sent): 
                        tgt_val.append(pred[pos,ids])

                for id_,translated_word in enumerate(val):
                    #Finds the gradient of a single sentence

                    if k == 1: 
                        IG.append(torch.sum(1/self.m*self.model.encoder.difference*grad(translated_word, self.model.encoder.emb, retain_graph=True,allow_unused=True)[0],2))
                        if debug : tgt_IG.append(torch.sum(1/self.m*self.model.encoder.difference*grad(tgt_val[id_], self.model.encoder.emb, retain_graph=True,allow_unused=True)[0],2))
                    else : 
                        IG[id_] += torch.sum(1/self.m*self.model.encoder.difference*grad(translated_word, self.model.encoder.emb, retain_graph=True,allow_unused=True)[0],2)
                        if debug: tgt_IG[id_] += torch.sum(1/self.m*self.model.encoder.difference*grad(tgt_val[id_], self.model.encoder.emb, retain_graph=True,allow_unused=True)[0],2)
            
            if debug:
                F.append({
                        "IG":IG,
                        "tgt_IG":tgt_IG,
                        "src_seq":src_seq,
                        "translated_sentence":translated_sentence,
                        "tgt_trans_sent":tgt_trans_sent
                    })
            else:
                F.append({
                    "IG":IG,
                    "src_seq":src_seq,
                    "translated_sentence":translated_sentence,
                })

            if dev:
                IG = torch.squeeze(torch.stack(IG)).detach().numpy().T
                if debug:
                    tgt_IG = torch.squeeze(torch.stack(tgt_IG)).detach().numpy().T
                    return IG,tgt_IG,src_seq,translated_sentence,tgt_trans_sent
                return IG,src_seq,translated_sentence
        return F

    def attributor_batch_beam(self,training_data,opt):
        def f(x):
            x.to(self.device)
            return x
        translator = Translator(opt)
        for batch in tqdm(training_data, mininterval=2, desc='  - (Attributing)', leave=False):
            src_seq, src_pos, tgt_seq, tgt_pos = map(f, batch)
            all_hyp, all_scores = translator.translate_batch(src_seq, src_pos,False) # translations and Beam search scores
            print(all_hyp)
            # for idx_seqs in all_hyp:
            #     for idx_seq in idx_seqs:
            #         print(grad(idx_seq, self.model.encoder.emb, retain_graph=True,allow_unused=True)[0])
            # print('[Info] Finished.') 

    def visualisation(self,IG,original_line,pred_line):
        fig = plt.figure(figsize=(8, 8.5))
        ax = fig.add_subplot(1, 1, 1)
        img = ax.imshow(IG, interpolation='nearest', cmap='gray')
        fig.colorbar(img, ax=ax)

        ax.set_yticks(range(len(original_line)))
        ax.set_yticklabels(original_line)

        ax.set_xticks(range(len(pred_line)))
        ax.set_xticklabels(pred_line, rotation=45)

        ax.set_xlabel('Output Sequence')
        ax.set_ylabel('Input Sequence')
        fig.show()
        plt.show()

if __name__ == "__main__":
    # Prepare DataLoader
    parser = argparse.ArgumentParser()
    
    parser.add_argument('-data', required=True)
    parser.add_argument('-batch_size', type=int, default=1)
    parser.add_argument('-m',type=int, default=10,
                        help='Resolution of the integrated gradient')
    parser.add_argument('-model', required=True,
                        help='Path to model .pt file')
    parser.add_argument('-out',help='Path to output file of ')
    parser.add_argument('-beam_size',default=5)
    parser.add_argument('-n_best', type=int, default=1,
                        help="""If verbose is set, will output the n_best
                        decoded sentences""")
    parser.add_argument('-return_attns', action='store_true')
    parser.add_argument('-debug', action='store_true')
    parser.add_argument('-no_cuda', action='store_true')
    parser.add_argument('-dev', action='store_true')

    opt = parser.parse_args()
    assert opt.dev or opt.out!=None, "You are not in dev mode but you don't have an output file"
    opt.cuda = not opt.no_cuda

    #========= Loading Dataset =========#
    data = torch.load(opt.data)

    training_data, validation_data = prepare_dataloaders(data, opt)
    attributor = Attribution(opt)
    #attributor.attributor_batch_beam(validation_data,opt)
    if opt.dev : 
        if opt.debug:
            IG,tgt_IG,src_seq,translated_sentence,tgt_trans_sent  = attributor.attribute_batch(validation_data,dev=True,debug=True)
            right_line = [validation_data.dataset.tgt_idx2word[idx.item()] for idx in tgt_trans_sent]
        else:
            IG,src_seq,translated_sentence = attributor.attribute_batch(validation_data,dev=True)

        original_line = [validation_data.dataset.src_idx2word[idx.item()] for idx in src_seq[0]]
        pred_line = [validation_data.dataset.tgt_idx2word[idx.item()] for idx in translated_sentence]

        attributor.visualisation(IG,original_line,pred_line)
        if opt.debug: attributor.visualisation(tgt_IG,original_line,right_line)

    else: 
        if not os.path.isfile(opt.out):
            outfile = open(opt.out,'wb')
            F = attributor.attribute_batch(validation_data,debug=opt.debug)
            pickle.dump(F,outfile)
            outfile.close()
        saved = open(opt.out,'rb')
        saved_file = pickle.load(saved)

        for dict_store in saved_file:
            if opt.debug:
                IG,tgt_IG,src_seq,translated_sentence,tgt_trans_sent  = dict_store["IG"],dict_store["tgt_IG"],dict_store["src_seq"],dict_store["translated_sentence"],dict_store["tgt_trans_sent"]
                tgt_IG = torch.squeeze(torch.stack(tgt_IG)).detach().numpy().T
            else:
                IG,src_seq,translated_sentence  = dict_store["IG"],dict_store["src_seq"],dict_store["translated_sentence"]

            IG = torch.squeeze(torch.stack(IG)).detach().numpy().T

            original_line = [validation_data.dataset.src_idx2word[idx.item()] for idx in src_seq[0]]
            pred_line = [validation_data.dataset.tgt_idx2word[idx.item()] for idx in translated_sentence]

            attributor.visualisation(IG,original_line,pred_line)
            if opt.debug:
                right_line = [validation_data.dataset.tgt_idx2word[idx.item()] for idx in tgt_trans_sent]
                attributor.visualisation(tgt_IG,original_line,right_line)
