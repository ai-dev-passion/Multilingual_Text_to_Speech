import json


class Params:

    @staticmethod
    def load_state_dict(d):
        for k, v in d.items(): setattr(Params, k, v)

    @staticmethod
    def state_dict():
        members = [attr for attr in dir(Params) if not callable(getattr(Params, attr)) and not attr.startswith("__")]
        return { k: Params.__dict__[k] for k in members }

    @staticmethod
    def load(json_path):
        with open(json_path, 'r', encoding='utf-8') as f:
            params = json.load(f)
            Params.load_state_dict(params)

    @staticmethod
    def save(json_path):
        with open(json_path, 'w', encoding='utf-8') as f:
            d = Params.state_dict()
            json.dump(d, f, indent=4)

    @staticmethod
    def symbols_count():
        symbols_count = len(Params.characters)
        if Params.use_phonemes: symbols_count = len(Params.phonemes)
        if Params.use_punctuation: symbols_count += len(Params.punctuations_out) + len(Params.punctuations_in)
        return symbols_count

    version = "1.0"

    # TRAINING:
    epochs = 300
    batch_size = 52
    learning_rate = 1e-3
    learning_rate_decay = 0.5
    learning_rate_decay_start = 15000
    learning_rate_decay_each = 15000
    weight_decay = 1e-6
    max_output_length = 5000
    gradient_clipping = 0.25
    reversal_gradient_clipping = 0.25 # used if reversal_classifier is True
    guided_attention_loss = True
    guided_attention_steps = 20000
    guided_attention_toleration = 0.25
    guided_attention_gain = 1.00025
    constant_teacher_forcing = True
    teacher_forcing = 1.0
    teacher_forcing_steps = 100000
    teacher_forcing_start_steps = 50000
    checkpoint_each_epochs = 10
    parallelization = "data"        # 'data' for Data Parallel (parallel batch), supports any number of GPUs
                                    # 'model' for Model Parallel (batch pipelining), supports exactly two GPUs
                                    #         encoder is moved to cuda:0 and the decoder is moved to cuda:1 device.  
    modelparallel_split_size = 13   # used if parallelization == 'model', the size of batch split for pipelining

    # MODEL:

    embedding_dimension = 512
    encoder_disabled = False 
    encoder_type = "simple" # one of: simple (single encoder for all languages without embedding), 
                            #         separate (distinct encoders for each language)
                            #         shared (single encoder for all languages with embedding)
                            #         convolutional (single grouped fully convolutional encoder without embedding, each group correspond to language)
    encoder_dimension = 512
    encoder_blocks = 3
    encoder_kernel_size = 5
    prenet_dimension = 256
    prenet_layers = 2
    attention_type = "location_sensitive"   # one of: location_sensitive (Tacotron 2 vanilla), 
                                            #         forward (undebugged, should allow just monotonous att.) 
                                            #         forward_transition_agent (undebugged, fwd with explicit transition agent)
    attention_dimension = 128
    attention_kernel_size = 31
    attention_location_dimension = 32
    decoder_dimension = 1024
    decoder_regularization = 'dropout'
    zoneout_hidden = 0.1
    zoneout_cell = 0.1
    dropout_hidden = 0.1
    postnet_dimension = 512
    postnet_blocks = 5
    postnet_kernel_size = 5
    dropout = 0.5   

    predict_linear = True
    cbhg_bank_kernels = 8
    cbhg_bank_dimension = 128
    cbhg_projection_kernel_size = 3
    cbhg_projection_dimension = 256
    cbhg_highway_dimension = 128
    cbhg_rnn_dim = 128
    cbhg_dropout = 0.0

    residual_encoder = False
    residual_latent_dimension = 16
    residual_blocks = 2
    residual_kernel_size = 3
    residual_dimension = 512
    residual_dropout = 0.0

    multi_speaker = False
    multi_language = False
    embedding_type = "simple"  # one of: simple (for usual lookup embedding), 
                               #         constant (returning a constant vector)
    speaker_embedding_dimension = 32
    # speaker_decoder_dimension = 64
    language_embedding_dimension = 32
    input_language_embedding = 4
    # language_decoder_dimension = 64
    speaker_number = 0
    language_number = 0
    reversal_classifier = False
    reversal_classifier_dim = 256

    stop_frames = 5  # number of frames at the end which are considered as "ending sequence"


    # DATASET:
    
    dataset = "ljspeech"        # one of: ljspeech, vctk, my_blizzard, mailabs
    cache_spectrograms = True
    languages = ['en-us']       # espeak format: phonemize --help
    balanced_sampling = False   # enables balanced sampling per languages (not speakers), multi_language must be True
    perfect_sampling = False    # used hust if balanced_sampling is True
                                # if True, each language has the same number of samples and these samples are grouped, batch_size must be divisible
                                # if False, samples are taken from the multinomial distr. with replacement

    # TEXT:

    characters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz '
    case_sensitive = True
    remove_multiple_wspaces = True 

    use_punctuation = True      # punctuations_{in, out} are valid only if True
    punctuations_out = '、。，"(),.:;¿?¡!\\'
    punctuations_in  = '\'-'

    # all phonemes of IPA: 'iyɨʉɯuɪʏʊeøɘəɵɤoɛœɜɞʌɔæɐaɶɑɒᵻʘɓǀɗǃʄǂɠǁʛpbtdʈɖcɟkɡqɢʔɴŋɲɳnɱmʙrʀⱱɾɽɸβfvθðszʃʒʂʐçʝxɣχʁħʕhɦɬɮʋɹɻjɰlɭʎʟˈˌːˑʍwɥʜʢʡɕʑɺɧ ɚ˞ɫ'
    use_phonemes = False   # phonemes are valid only if True
    phonemes = 'ɹɐpbtdkɡfvθðszʃʒhmnŋlrwjeəɪɒuːɛiaʌʊɑɜɔx '
   
    # AUDIO:

    # ljspeech    - 22050, 1102
    # vctk        - 48000, 2400
    # my_blizzard - 44100, 2250
    sample_rate = 22050 
    num_fft = 1102
    num_mels = 80
    num_mfcc = 13 # just just for an objective metric computation

    stft_window_ms = 50
    stft_shift_ms = 12.5
    
    griffin_lim_iters = 50
    griffin_lim_power = 1.5 

    normalize_spectrogram = True 

    use_preemphasis = True
    preemphasis = 0.97 
