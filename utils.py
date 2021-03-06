import numpy as np
import librosa
import os, copy
from scipy import signal
import hyperparams as hp
import torch as t

def get_spectrograms(fpath):
    '''Parse the wave file in `fpath` and
    Returns normalized melspectrogram and linear spectrogram.
    Args:
      fpath: A string. The full path of a sound file.
    Returns:
      mel: A 2d array of shape (T, n_mels) and dtype of float32.
      mag: A 2d array of shape (T, 1+n_fft/2) and dtype of float32.
    '''
    fmin = 50
    fmax = 11000
    # Loading sound file
    y, sr = librosa.load(fpath, sr=hp.sr)
    y = y / (max(y))*0.999
    yt, trim_index = librosa.effects.trim(y, top_db=20, frame_length=800, hop_length=200)	# no trim, just to get the trim index
    y = y[max(trim_index[0]-4800,0):min(trim_index[1]+4800,len(y))]

    # Preemphasis
#    y = np.append(y[0], y[1:] - hp.preemphasis * y[:-1])

    # stft
    linear = librosa.stft(y=y,
                          n_fft=hp.n_fft,
                          hop_length=hp.hop_length,
                          win_length=hp.win_length)

    # magnitude spectrogram
    mag = np.abs(linear)  # (1+n_fft//2, T)		# spc
    # mel spectrogram
    mel_basis = librosa.filters.mel(hp.sr, hp.n_fft, hp.n_mels, fmin, fmax)  # (n_mels, 1+n_fft//2)
    mel = np.dot(mel_basis, mag)
    mel = 20*np.log10(np.maximum(1e-5, mel))
    mag = 20*np.log10(np.maximum(1e-5, mag))

    mel = np.maximum((mel + hp.max_db - hp.ref_db)	/ hp.max_db, 1e-8)
    mag = np.maximum((mag + hp.max_db - hp.ref_db)	/ hp.max_db, 1e-8)

    # Transpose
    mel = mel.T.astype(np.float32)  # (T, n_mels)
    mag = mag.T.astype(np.float32)  # (T, 1+n_fft//2)

    #mel = mel * 8. - 4.
    return mel, mag

def invert_spectrogram(spectrogram):
    '''Applies inverse fft.
    Args:
      spectrogram: [1+n_fft//2, t]
    '''
    return librosa.istft(spectrogram, hp.hop_length, win_length=hp.win_length, window="hann")

def get_positional_table(d_pos_vec, n_position=1024):
    position_enc = np.array([
        [pos / np.power(10000, 2*i/d_pos_vec) for i in range(d_pos_vec)]
        if pos != 0 else np.zeros(d_pos_vec) for pos in range(n_position)])

    position_enc[1:, 0::2] = np.sin(position_enc[1:, 0::2]) # dim 2i
    position_enc[1:, 1::2] = np.cos(position_enc[1:, 1::2]) # dim 2i+1
    return t.from_numpy(position_enc).type(t.FloatTensor)

def get_sinusoid_encoding_table(n_position, d_hid, padding_idx=None):
    ''' Sinusoid position encoding table '''

    def cal_angle(position, hid_idx):
        return position / np.power(10000, 2 * (hid_idx // 2) / d_hid)

    def get_posi_angle_vec(position):
        return [cal_angle(position, hid_j) for hid_j in range(d_hid)]

    sinusoid_table = np.array([get_posi_angle_vec(pos_i) for pos_i in range(n_position)])

    sinusoid_table[:, 0::2] = np.sin(sinusoid_table[:, 0::2])  # dim 2i
    sinusoid_table[:, 1::2] = np.cos(sinusoid_table[:, 1::2])  # dim 2i+1

    if padding_idx is not None:
        # zero vector for padding dimension
        sinusoid_table[padding_idx] = 0.

    return t.FloatTensor(sinusoid_table)

def build_kv_mask(end_pt_preds, decoder_len, encoder_len):  # [B, T]
    cum_end_pt_idx = t.cumsum(end_pt_preds)     # [0,0,...,0,1,1,...,1,2,2,...,2,3,3,..,145,146,...,146]
    mask = t.ones_like(end_pt_preds).unsqueeze(-1).repeat(1, 1, encoder_len)
    for i in range (mask.size(0)):
        for j in range(mask.size(1)):
            mask[i,j,cum_end_pt_idx[i,j]:cum_end_pt_idx[i,j]+4]=0

    return mask

def update_kv_mask(mask, attn_probs):	# mask : [B, t', N], attn_probs : [3, t', N]
    stop_flag=False
    attn_probs_new = []
    for i in range(len(attn_probs)):
        attn_probs_new.append(attn_probs[i].unsqueeze(0))
    attn_probs = t.cat(attn_probs_new, 0)
    attn_probs = attn_probs.contiguous().view(attn_probs.size(0), 1, hp.n_heads, attn_probs.size(2), attn_probs.size(3))
    attn_probs = attn_probs.permute(1, 0, 2, 3, 4)	# [B, 3, 4, t', N]
    attn_probs = attn_probs[:, 0, -2:, :, :]	# [B, 2, t', N]		# only the 1st layer 3,4 head
    attn_probs = t.sum(attn_probs, 1)		# [B, t', N]
    attn_probs = t.argmax(attn_probs, -1)	# [B, t']
    new_start_index = attn_probs[:, -1]	#[B]
    new_mask = t.zeros(mask.size(0), 1, mask.size(-1)).cuda()	# [B, 1, N]
	
    for i in range(len(new_mask)):
        new_mask[i, :, new_start_index[i]:new_start_index[i]+3] = 1

    new_mask = new_mask.eq(0)	# [B, 1, N]
    mask = t.cat((mask, new_mask), 1)	# [B, t'+1, N]
    if new_start_index == mask.size(-1)-1:
        stop_flag=True
    return mask, stop_flag
