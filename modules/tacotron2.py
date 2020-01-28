import math
import torch
from torch.nn import functional as F
from torch.nn import Sequential, ModuleList, Linear, ReLU, Dropout, LSTM, Embedding

from utils import lengths_to_mask
from modules.layers import ZoneoutLSTMCell, DropoutLSTMCell, ConvBlock, ConstantEmbedding
from modules.attention import LocationSensitiveAttention, ForwardAttention, ForwardAttentionWithTransition
from modules.encoder import Encoder, MultiEncoder, ConditionalEncoder, ConvolutionalEncoder #, GeneratedEncoder
from modules.cbhg import PostnetCBHG
from modules.classifier import ReversalClassifier
from modules.residual_encoder import ResidualEncoder
from params.params import Params as hp


class Prenet(torch.nn.Module):
    """
    Prenet:
        stack of 2 linear layers with dropout which is enabled even during inference (output variation)
        should act as bottleneck for attention

    Arguments:
        input_dim -- size of the input (supposed the number of frame mels)
        output_dim -- size of the output
        num_layers -- number of the linear layers (at least one)
        dropout -- dropout rate to be aplied after each layer (even during inference)
    """

    def __init__(self, input_dim, output_dim, num_layers, dropout):
        super(Prenet, self).__init__()
        assert num_layers > 0, ('There must be at least one layer in the pre-net.')
        self._dropout_rate = dropout
        self._activation = ReLU()
        layers = [Linear(input_dim, output_dim)] + [Linear(output_dim, output_dim) for _ in range(num_layers - 1)]
        self._layers = ModuleList(layers)

    def _layer_pass(self, x, layer):
        x = layer(x)
        x = self._activation(x)
        x = F.dropout(x, p=self._dropout_rate, training=True)
        return x

    def forward(self, x):
        for layer in self._layers:
            x = self._layer_pass(x, layer)
        return x


class Postnet(torch.nn.Module):
    """
    Postnet:
        stack of 5 conv. layers 5 × 1 with BN and tanh (except last), dropout

    Arguments:
        input_dimension -- size of the input and output (supposed the number of frame mels)
        postnet_dimension -- size of the internal convolutional blocks
        num_blocks -- number of the convolutional blocks (at least one)
        kernel_size -- kernel size of the encoder's convolutional blocks
        dropout -- dropout rate to be aplied after each convolutional block
    """

    def __init__(self, input_dimension, postnet_dimension, num_blocks, kernel_size, dropout):
        super(Postnet, self).__init__()
        assert num_blocks > 1, ('There must be at least two convolutional blocks in the post-net.')
        self._convs = Sequential(
            ConvBlock(input_dimension, postnet_dimension, kernel_size, dropout, 'tanh'),
            *[ConvBlock(postnet_dimension, postnet_dimension, kernel_size, dropout, 'tanh') for _ in range(num_blocks - 2)],
            ConvBlock(postnet_dimension, input_dimension, kernel_size, dropout, 'identity')
        )

    def forward(self, x, x_lengths):
        residual = x
        x = self._convs(x)
        x += residual   
        return x


class Decoder(torch.nn.Module):
    """
    Decoder:
        stack of 2 uni-directional LSTM layers with 1024 units
        first LSTM is used to query attention mechanism
        input of the first LSTM is previous prediction (pre-net output) and previous context vector
        second LSTM acts as a generator
        input of the second LSTM is current context vector and output of the first LSTM
        output is passed through stop token layer, frame prediction layer and pre-ne

    Arguments:
        output_dim -- size of the predicted frame, i.e. number of mels 
        decoder_dim -- size of the generator output (and also of all the LSTMs used in the decoder)
        attention -- instance of the location-sensitive attention module 
        generator_rnn -- instance of generator RNN 
        attention_rnn -- instance of attention RNN
        context_dim -- size of the context vector produced by the given attention
        prenet -- instance of the pre-net module
        prenet_dim -- output dimension of the pre-net
        max_length -- maximal length of the input sequence
        max_frames -- maximal number of the predicted frames
    """

    def __init__(self, output_dim, decoder_dim, attention, generator_rnn, attention_rnn, context_dim, prenet, prenet_dim, max_frames):
        super(Decoder, self).__init__()
        self._prenet = prenet
        self._attention = attention
        self._output_dim = output_dim
        self._decoder_dim = decoder_dim
        self._max_frames = max_frames
        self._attention_lstm = attention_rnn
        self._generator_lstm = generator_rnn
        self._frame_prediction = Linear(context_dim + decoder_dim, output_dim)
        self._stop_prediction = Linear(context_dim + decoder_dim, 1)
        if hp.multi_speaker:
            self._speaker_embedding = self._get_embedding(hp.embedding_type, hp.speaker_embedding_dimension, hp.speaker_number)
            # self._speaker_decoder = Linear(hp.speaker_embedding_dimension, hp.speaker_decoder_dimension)
        if hp.multi_language:
            self._language_embedding = self._get_embedding(hp.embedding_type, hp.language_embedding_dimension, len(hp.languages))
            # self._language_decoder = Linear(hp.language_embedding_dimension, hp.language_decoder_dimension)

    def _get_embedding(self, name, embedding_dimension, size=None):
        if name == "simple":
            embedding = Embedding(size, embedding_dimension)
            torch.nn.init.xavier_uniform_(embedding.weight)
            return embedding
        elif name == "constant":
            return ConstantEmbedding(torch.zeros(embedding_dimension))  

    def _target_init(self, target, batch_size):
        """Prepend target spectrogram with a zero frame and pass it through pre-net."""
        # the F.pad function has some issues: https://github.com/pytorch/pytorch/issues/13058
        first_frame = torch.zeros(batch_size, self._output_dim, device=target.device).unsqueeze(1)
        target = target.transpose(1, 2) # [B, F, N_MEL]
        target = torch.cat((first_frame, target), dim=1)
        target = self._prenet(target)
        return target

    def _decoder_init(self, batch_size, device):
        """Initialize hidden and cell state of the deocder's RNNs."""
        h_att = torch.zeros(batch_size, self._decoder_dim, device=device)
        c_att = torch.zeros(batch_size, self._decoder_dim, device=device)
        h_gen = torch.zeros(batch_size, self._decoder_dim, device=device)
        c_gen = torch.zeros(batch_size, self._decoder_dim, device=device)
        return h_att, c_att, h_gen, c_gen

    def _add_conditional_embedding(self, encoded, layer, condition):
        """Compute speaker (lang.) embedding and concat it to the encoder output."""
        embedded = layer(encoded if condition is None else condition)
        expanded = embedded.unsqueeze(1).expand((-1, encoded.shape[1], -1))
        return torch.cat((encoded, expanded), dim=-1) 

    def _decode(self, encoded_input, mask, target, teacher_forcing_ratio, speaker, language):
        """Perform decoding of the encoded input sequence."""

        batch_size = encoded_input.size(0)
        max_length = encoded_input.size(1)
        inference = target is None
        max_frames = self._max_frames if inference else target.size(2) 
        input_device = encoded_input.device

        # obtain speaker and language embeddings (or a dummy tensor)
        if hp.multi_speaker:
            encoded_input = self._add_conditional_embedding(encoded_input, self._speaker_embedding, speaker)
        if hp.multi_language:
            encoded_input = self._add_conditional_embedding(encoded_input, self._language_embedding, language)
        
        # attention and decoder states initialization  
        context = self._attention.reset(encoded_input, batch_size, max_length, input_device)
        h_att, c_att, h_gen, c_gen = self._decoder_init(batch_size, input_device)      
        
        # prepare some inference or train specific variables (teacher forcing, max. predicted length)
        frame = torch.zeros(batch_size, self._output_dim, device=input_device) 
        if not inference:
            target = self._target_init(target, batch_size)  
            teacher = torch.rand([max_frames], device=input_device) > (1 - teacher_forcing_ratio)
        
        # tensors for storing output
        spectrogram = torch.zeros(batch_size, max_frames, self._output_dim, device=input_device)
        alignments = torch.zeros(batch_size, max_frames, max_length, device=input_device)
        stop_tokens = torch.zeros(batch_size, max_frames, 1, device=input_device)
        
        # decoding loop
        stop_frames = -1
        for i in range(max_frames):
            prev_frame = self._prenet(frame) if inference or not teacher[i] else target[:,i]

            # run decoder attention and RNNs
            attention_input = torch.cat((prev_frame, context), dim=1)
            h_att, c_att = self._attention_lstm(attention_input, h_att, c_att)
            context, weights = self._attention(h_att, encoded_input, mask, prev_frame)
            generator_input = torch.cat((h_att, context), dim=1) # , speaker_embedding, language_embedding
            h_gen, c_gen = self._generator_lstm(generator_input, h_gen, c_gen)
            
            # predict frame and stop token
            proto_output = torch.cat((h_gen, context), dim=1)
            frame = self._frame_prediction(proto_output)
            stop_logits = self._stop_prediction(proto_output)
            
            # store outputs
            spectrogram[:,i] = frame
            alignments[:,i] = weights
            stop_tokens[:,i] = stop_logits
            
            # stop decoding if predicted (just during inference)
            if inference and torch.sigmoid(stop_logits).ge(0.5):
                if stop_frames == -1: 
                    stop_frames = hp.stop_frames
                    continue
                stop_frames -= 1
                if stop_frames == 0:
                    return spectrogram[:,:i+1], stop_tokens[:,:i+1].squeeze(2), alignments[:,:i+1]
        
        return spectrogram, stop_tokens.squeeze(2), alignments

    def forward(self, encoded_input, encoded_lenghts, target, teacher_forcing_ratio, speaker, language):
        ml = encoded_input.size(1)
        mask = lengths_to_mask(encoded_lenghts, max_length=ml)
        return self._decode(encoded_input, mask, target, teacher_forcing_ratio, speaker, language)

    def inference(self, encoded_input, speaker, language):
        mask = lengths_to_mask(torch.LongTensor([encoded_input.size(1)]))
        spectrogram, _, _ = self._decode(encoded_input, mask, None, 0.0, speaker, language)
        return spectrogram
     

class Tacotron(torch.nn.Module):
    """
    Tacotron 2:
        characters as learned embedding
        encoder, attention, decoder which predicts frames of mel spectrogram
        the predicted mel spectrogram is passed through post-net which
          predicts a residual to add to the prediction
        minimize MSE from before and after the post-net to aid convergence
    """

    def __init__(self):
        super(Tacotron, self).__init__()

        if not hp.encoder_disabled:

            # Encoder embedding 
            other_symbols = 3 # PAD, EOS, UNK
            self._embedding = Embedding(
                                hp.symbols_count() + other_symbols, 
                                hp.embedding_dimension, 
                                padding_idx=0
                            )
            torch.nn.init.xavier_uniform_(self._embedding.weight)

            # Encoder transforming graphmenes or phonemes into abstract input representation
            self._encoder = self._get_encoder(hp.encoder_type)

        # Reversal language classifier to make encoder truly languagge independent
        if hp.reversal_classifier:
            self._reversal_classifier = ReversalClassifier(
                hp.encoder_dimension, 
                hp.reversal_classifier_dim, 
                hp.language_number,
                hp.reversal_gradient_clipping
            )

        # Prenet for transformation of previous predicted frame
        self._prenet = Prenet(
                        hp.num_mels, 
                        hp.prenet_dimension, 
                        hp.prenet_layers, 
                        hp.dropout
                    )     

        if hp.residual_encoder:
            self._residual_encoder = ResidualEncoder(
                                        hp.num_mels,
                                        hp.residual_dimension,
                                        hp.residual_latent_dimension,
                                        hp.residual_blocks,
                                        hp.residual_kernel_size,
                                        hp.residual_dropout
                                    )

        # Speaker and language embeddings and latent vector make decoder bigger
        generator_input_dimension = 0
        decoder_input_dimension = hp.encoder_dimension
        if hp.multi_speaker:
            # generator_input_dimension += hp.speaker_decoder_dimension
            decoder_input_dimension += hp.speaker_embedding_dimension
        if hp.multi_language:
            # generator_input_dimension += hp.language_decoder_dimension
            decoder_input_dimension += hp.language_embedding_dimension
        if hp.residual_encoder:
            decoder_input_dimension += hp.residual_latent_dimension

        # Decoder attention layer 
        self._attention = self._get_attention(hp.attention_type, decoder_input_dimension)
        
        # Instantiate decoder RNN layers
        gen_cell_dimension = generator_input_dimension + decoder_input_dimension + hp.decoder_dimension
        att_cell_dimension = decoder_input_dimension + hp.prenet_dimension
        if hp.decoder_regularization == 'zoneout':
            generator_rnn = ZoneoutLSTMCell(gen_cell_dimension, hp.decoder_dimension, hp.zoneout_hidden, hp.zoneout_cell) 
            attention_rnn = ZoneoutLSTMCell(att_cell_dimension, hp.decoder_dimension, hp.zoneout_hidden, hp.zoneout_cell)
        else:
            generator_rnn = DropoutLSTMCell(gen_cell_dimension, hp.decoder_dimension, hp.dropout_hidden) 
            attention_rnn = DropoutLSTMCell(att_cell_dimension, hp.decoder_dimension, hp.dropout_hidden)

        # Decoder which controls attention and produces mel frames and stop tokens 
        self._decoder = Decoder(
                        hp.num_mels, 
                        hp.decoder_dimension, 
                        self._attention, 
                        generator_rnn,
                        attention_rnn, 
                        decoder_input_dimension,
                        self._prenet, 
                        hp.prenet_dimension,
                        hp.max_output_length
                    )      

        # Postnet transforming predicted mel frames (residual mel or linear frames)
        if hp.predict_linear:
            self._postnet = PostnetCBHG(
                            hp.num_mels, 
                            hp.num_fft // 2 + 1, 
                            hp.cbhg_bank_kernels, 
                            hp.cbhg_bank_dimension, 
                            hp.cbhg_projection_dimension, 
                            hp.cbhg_projection_kernel_size,
                            hp.cbhg_highway_dimension, 
                            hp.cbhg_rnn_dim,
                            hp.cbhg_dropout
                        )
        else:
            self._postnet = Postnet(
                            hp.num_mels, 
                            hp.postnet_dimension,
                            hp.postnet_blocks, 
                            hp.postnet_kernel_size, 
                            hp.dropout
                        )

    def _get_encoder(self, name):
        args = (hp.embedding_dimension,
                hp.encoder_dimension, 
                hp.encoder_blocks,
                hp.encoder_kernel_size,
                hp.dropout)
        if name == "simple":
            return Encoder(*args)
        elif name == "separate":
            return MultiEncoder(hp.language_number, args)  
        elif name == "shared":
            return ConditionalEncoder(hp.language_number, hp.input_language_embedding, args)
        elif name == "convolutional":
            return ConvolutionalEncoder(hp.embedding_dimension, hp.encoder_dimension, 0.05, 1 if not hp.multi_language else hp.language_number)
           
    def _get_attention(self, name, memory_dimension):
        args = (hp.attention_dimension,
                hp.decoder_dimension, 
                memory_dimension)
        if name == "location_sensitive":
            return LocationSensitiveAttention(
                hp.attention_kernel_size,
                hp.attention_location_dimension,
                False,
                *args)
        elif name == "forward":
            return ForwardAttention(*args)
        elif name == "forward_transition_agent":
            return ForwardAttentionWithTransition(
                hp.prenet_dimension,
                *args)

    def forward(self, text, text_length, target, target_length, speakers, languages, teacher_forcing_ratio=0.0): 

        if not hp.encoder_disabled:
            embedded = self._embedding(text)
            encoded = self._encoder(embedded, text_length, languages)
        else:
            encoded = torch.zeros([text.shape[0], text.shape[1], hp.encoder_dimension], device=text.device)

        lang_prediction = self._reversal_classifier(encoded) if hp.reversal_classifier else None
        latent_mean, latent_var = None, None
        if hp.residual_encoder:
            latent, latent_mean, latent_var = self._residual_encoder(target, target_length)
            expanded_latent = latent.unsqueeze(1).expand((-1, encoded.shape[1], -1))
            encoded = torch.cat((encoded, expanded_latent), dim=-1) 
          
        decoded = self._decoder(encoded, text_length, target, teacher_forcing_ratio, speakers, languages)
        prediction, stop_token, alignment = decoded
        pre_prediction = prediction.transpose(1,2)
        post_prediction = self._postnet(pre_prediction, target_length)

        # mask output paddings
        target_mask = lengths_to_mask(target_length, target.size(2))
        stop_token.masked_fill_(~target_mask, 1000)
        target_mask = target_mask.unsqueeze(1).float()
        pre_prediction = pre_prediction * target_mask
        post_prediction = post_prediction * target_mask

        return post_prediction, pre_prediction, stop_token, alignment, lang_prediction, latent_mean, latent_var

    def inference(self, text, speaker=None, language=None):
        # Pretend having a batch of size 1
        text.unsqueeze_(0)
        
        # Encode input
        if not hp.encoder_disabled:
            embedded = self._embedding(text)
            encoded = self._encoder(embedded, torch.LongTensor([text.size(1)]), language)
        else:
            encoded = torch.zeros([text.shape[0], text.shape[1], hp.encoder_dimension], device=text.device)

        # Add a latent vector (sampling or mean)
        if hp.residual_encoder:
            latent = self._residual_encoder.inference(1)
            expanded_latent = latent.unsqueeze(1).expand((-1, encoded.shape[1], -1))
            encoded = torch.cat((encoded, expanded_latent), dim=-1) 

        # Decode with respect to speaker and language embeddings
        prediction = self._decoder.inference(encoded, speaker, language)

        # Post process generated spectrogram
        prediction = prediction.transpose(1,2)
        post_prediction = self._postnet(prediction, torch.LongTensor([prediction.size(2)]))
        return post_prediction.squeeze(0)


class ModelParallelTacotron(Tacotron):
    def __init__(self, *args, **kwargs):
        super(ModelParallelTacotron, self).__init__()
        assert hp.batch_size % hp.modelparallel_split_size == 0, ('Batch size must be divisible by model parallel split size.')
        self._split_size = hp.modelparallel_split_size
        self._gpu1 = torch.device("cuda:0")
        self._gpu2 = torch.device("cuda:1")
        self._embedding.to(self._gpu1)
        self._encoder.to(self._gpu1)
        self._prenet.to(self._gpu2)
        self._attention.to(self._gpu2)
        self._decoder.to(self._gpu2)
        self._postnet.to(self._gpu2)

    def _encoder_forward(self, splits):
        embedded = self._embedding(splits[0])
        return self._encoder(embedded, splits[1], splits[5])

    def _decoder_forward(self, encoded, splits, ret, teacher_forcing_ratio):
        encoded = encoded.to(self._gpu2)
        encoded_length = splits[1].to(self._gpu2)
        speaker = None if splits[4] is None else splits[4].to(self._gpu2)
        language = None if splits[5] is None else splits[5].to(self._gpu2)
        
        decoded = self._decoder(encoded, encoded_length, splits[2], teacher_forcing_ratio, speaker, language)
        prediction, stop_token, alignment = decoded
        pre_prediction = prediction.transpose(1,2)
        post_prediction = self._postnet(pre_prediction, splits[3])

        target_mask = lengths_to_mask(splits[3], splits[2].size(2))
        stop_token.masked_fill_(~target_mask, 1000)
        target_mask = target_mask.unsqueeze(1).float()
        pre_prediction = pre_prediction * target_mask
        post_prediction = post_prediction * target_mask

        ret[0].append(post_prediction)
        ret[1].append(pre_prediction)
        ret[2].append(stop_token)
        ret[3].append(alignment)

    def forward(self, text, text_length, target, target_length, speakers, languages, teacher_forcing_ratio=0.0):
       
        batch = [text, text_length, target, target_length, speakers, languages]
        ns = math.ceil(text.size(0) / self._split_size)
        batch_splits = [iter([None]*ns) if x is None else iter(x.split(self._split_size, dim=0)) for x in batch]
        
        ret = [[],[],[],[]]

        next_splits = next(zip(*batch_splits))
        encoded = self._encoder_forward(next_splits)
        prev_splits = next_splits

        for next_splits in zip(*batch_splits):
            self._decoder_forward(encoded, prev_splits, ret, teacher_forcing_ratio)
            encoded = self._encoder_forward(next_splits)
            prev_splits = next_splits

        self._decoder_forward(encoded, prev_splits, ret, teacher_forcing_ratio)

        return tuple([torch.cat(x) for x in ret])


class TacotronLoss(torch.nn.Module):
    """Wrapper around loss functions.
    
    - L2 or L1 of the prediction before and after the postnet.
    - Cross entropy of the stop tokens (non masked)
    - Guided attention loss:
        prompt the attention matrix to be nearly diagonal, this is how people usualy read text
        introduced by 'Efficiently Trainable Text-to-Speech System Based on Deep Convolutional Networks with Guided Attention'
    """

    def __init__(self, guided_att_steps, guided_att_variance, guided_att_gamma, num_languages=0):
        super(TacotronLoss, self).__init__()
        self._g = guided_att_variance
        self._gamma = guided_att_gamma
        self._g_steps = guided_att_steps
        self._num_languages = num_languages

    def update_states(self):
        self._g *= self._gamma
        self._g_steps = max(0, self._g_steps - 1)

    def _guided_attention(self, alignments, input_lengths, target_lengths):
        if not hp.guided_attention_loss or self._g_steps == 0: return 0
        input_device = alignments.device
        weights = torch.zeros_like(alignments)
        for i, (f, l) in enumerate(zip(target_lengths, input_lengths)):
            grid_f, grid_l = torch.meshgrid(torch.arange(f, dtype=torch.float, device=input_device), torch.arange(l, dtype=torch.float, device=input_device))
            weights[i, :f, :l] = 1 - torch.exp(-(grid_l/l - grid_f/f)**2 / (2 * self._g ** 2)) 
        loss = torch.sum(weights * alignments, dim=(1,2))
        loss = torch.mean(loss / target_lengths.float())
        return loss

    def forward(self, source_length, target_length, pre_prediction, pre_target, post_prediction, post_target, stop, target_stop, alignment, lang, lang_prediction, pre_mean, pre_var):
        pre_target.requires_grad = False
        post_target.requires_grad = False
        target_stop.requires_grad = False
        
        stop_balance = torch.tensor([100], device=stop.device, dtype=torch.float32)
        losses = {
            'mel_pre' : 2 * F.mse_loss(pre_prediction, pre_target),
            'mel_pos' : F.mse_loss(post_prediction, post_target),
            'stop_token' : F.binary_cross_entropy_with_logits(stop, target_stop, pos_weight=stop_balance) / (hp.num_mels + 2),
        }

        if hp.reversal_classifier:
            losses['lang_class'] = ReversalClassifier.loss(source_length, lang, lang_prediction) / (hp.num_mels + 2)

        if hp.guided_attention_loss: 
            losses['guided_att'] = self._guided_attention(alignment, source_length, target_length)

        if hp.residual_encoder:
            losses['latent'] = hp.residual_latent_dimension * ResidualEncoder.loss(pre_mean, pre_var)

        return sum(losses.values()), losses
