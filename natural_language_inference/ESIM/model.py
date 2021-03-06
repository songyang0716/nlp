# Reference code: https://github.com/coetaur0/ESIM

import torch
import torch.nn as nn
# import torchvision.transforms as T
import torch.nn.functional as F


def get_mask(sequences_batch, sequences_lengths):
	"""
	Get the mask for a batch of padded variable length sequences.
	Args:
		sequences_batch: A batch of padded variable length sequences
			containing word indices. Must be a 2-dimensional tensor of size
			(batch, sequence).
		sequences_lengths: A tensor containing the lengths of the sequences in
			'sequences_batch'. Must be of size (batch).
	Returns:
		A mask of size (batch, max_sequence_length), where max_sequence_length
		is the length of the longest sequence in the batch. padding cells are 0, non-padding cells are 1
	"""
	batch_size = sequences_batch.size()[0]
	max_length = torch.max(sequences_lengths)
	mask = torch.ones(batch_size, max_length, dtype=torch.float)
	mask[sequences_batch[:, :max_length] == 0] = 0.0
	return mask


# https://github.com/allenai/allennlp/blob/master/allennlp/nn/util.py.
def masked_softmax(tensor, mask):
	"""
	Apply a masked softmax on the last dimension of a tensor.
	The input tensor and mask should be of size (batch, *, sequence_length).
	Args:
		tensor: The tensor on which the softmax function must be applied along
			the last dimension.
		mask: A mask of the same size as the tensor with 0s in the positions of
			the values that must be masked and 1s everywhere else.
	Returns:
		A tensor of the same size as the inputs containing the result of the
		softmax.
	"""
	# tensor size is batch * sequence_hy * sequence_pre (from the first masked_softmax function)
	tensor_shape = tensor.size()

	# reshape size _ * sequence_pre
	reshaped_tensor = tensor.view(-1, tensor_shape[-1])

	# Reshape the mask so it matches the size of the input tensor.
	# mask dim is batch * sequence_pre
	while mask.dim() < tensor.dim():
		mask = mask.unsqueeze(1)

	# reshape mask with shape batch * sequence_hy * sequence_pre
	# in this case, for each dim of sequence_hy, should have a copy of mask
	mask = mask.expand_as(tensor).contiguous().float()
	# mask is _ * sequence_pre
	reshaped_mask = mask.view(-1, mask.size()[-1])

	result = nn.functional.softmax(reshaped_tensor * reshaped_mask, dim=-1)
	result = result * reshaped_mask
	# 1e-13 is added to avoid divisions by zero.
	result = result / (result.sum(dim=-1, keepdim=True) + 1e-13)

	return result.view(*tensor_shape)
	


# Code widely inspired from:
# https://github.com/allenai/allennlp/blob/master/allennlp/nn/util.py.
def weighted_sum(tensor, weights, mask):
	"""
	Apply a weighted sum on the vectors along the last dimension of 'tensor',
	and mask the vectors in the result with 'mask'.
	Args:
		tensor: A tensor of vectors on which a weighted sum must be applied.
		weights: The weights to use in the weighted sum.
		mask: A mask to apply on the result of the weighted sum.
	Returns:
		A new tensor containing the result of the weighted sum after the mask
		has been applied on it.
	"""
	weighted_sum = weights.bmm(tensor)

	while mask.dim() < weighted_sum.dim():
		mask = mask.unsqueeze(1)
	mask = mask.transpose(-1, -2)
	mask = mask.expand_as(weighted_sum).contiguous().float()

	return weighted_sum * mask



class Seq2SeqEncoder(nn.Module):
	"""
	RNN taking variable length padded sequences of vectors as input and
	encoding them into padded sequences of vectors of the same length.
	This module is useful to handle batches of padded sequences of vectors
	that have different lengths and that need to be passed through a RNN.
	The sequences are sorted in descending order of their lengths, packed,
	passed through the RNN, and the resulting sequences are then padded and
	permuted back to the original order of the input sequences.
	"""
	def __init__(self,
				 input_size,
				 hidden_size,
				 num_layers=1,
				 bias=True,
				 dropout=0.0,
				 bidirectional=False):
		"""
		Args:
			rnn_type: The type of RNN to use as encoder in the module.
				Must be a class inheriting from torch.nn.RNNBase
				(such as torch.nn.LSTM for example).
			input_size: The number of expected features in the input of the
				module.
			hidden_size: The number of features in the hidden state of the RNN
				used as encoder by the module.
			num_layers: The number of recurrent layers in the encoder of the
				module. Defaults to 1.
			bias: If False, the encoder does not use bias weights b_ih and
				b_hh. Defaults to True.
			dropout: If non-zero, introduces a dropout layer on the outputs
				of each layer of the encoder except the last one, with dropout
				probability equal to 'dropout'. Defaults to 0.0.
			bidirectional: If True, the encoder of the module is bidirectional.
				Defaults to False.
		"""
		super(Seq2SeqEncoder, self).__init__()

		self.input_size = input_size
		self.hidden_size = hidden_size
		self.num_layers = num_layers
		self.dropout = dropout
		self.bidirectional = bidirectional

		# if batch_first=True, input as (batch, seq, feature)
		self._lstm = nn.LSTM(input_size=self.input_dim,
							 hidden_size=self.hidden_dim,
							 num_layers=self.num_layers,
							 dropout=self.dropout,
							 batch_first=True,
							 bidirectional=self.bidirectional)


class SoftmaxAttention(nn.Module):
	"""
	Attention layer taking premises and hypotheses encoded by an RNN as input
	and computing the soft attention between their elements.
	The dot product of the encoded vectors in the premises and hypotheses is
	first computed. The softmax of the result is then used in a weighted sum
	of the vectors of the premises for each element of the hypotheses, and
	conversely for the elements of the premises.
	"""
	def forward(self,
				premise_batch,
				premise_mask,
				hypothesis_batch,
				hypothesis_mask):
		"""
		Args:
			premise_batch: A batch of sequences of vectors representing the
				premises in some NLI task. The batch is assumed to have the
				size (batch, sequences, vector_dim).
			premise_mask: A mask for the sequences in the premise batch, to
				ignore padding data in the sequences during the computation of
				the attention.
			hypothesis_batch: A batch of sequences of vectors representing the
				hypotheses in some NLI task. The batch is assumed to have the
				size (batch, sequences, vector_dim).
			hypothesis_mask: A mask for the sequences in the hypotheses batch,
				to ignore padding data in the sequences during the computation
				of the attention.
		Returns:
			attended_premises: The sequences of attention vectors for the
				premises in the input batch.
			attended_hypotheses: The sequences of attention vectors for the
				hypotheses in the input batch.
		"""		
		# Dot product between premises and hypotheses in each sequence of
		# the batch
		similarity_matrix = premise_batch.bmm(hypothesis_batch.transpose(2, 1))

		# Softmax attention weights.
		prem_hyp_attn = masked_softmax(similarity_matrix, hypothesis_mask)
		hyp_prem_attn = masked_softmax(similarity_matrix.transpose(1, 2), premise_mask)
	 
		# Weighted sums of the hypotheses for the the premises attention,
		# and vice-versa for the attention of the hypotheses.
		attended_premises = weighted_sum(hypothesis_batch,
										 prem_hyp_attn,
										 premise_mask)

		attended_hypotheses = weighted_sum(premise_batch,
										   hyp_prem_attn,
										   hypothesis_mask)

		return attended_premises, attended_hypotheses
		

class ESIM(nn.Module):
	"""
		Implementation of CNN for classification
	"""
	def __init__(self, 
				 vocab_size,
				 embeddings_dim,
				 hidden_size,
				 embeddings,
				 dropout,
				 num_classes,
				 device):
		super(ESIM, self).__init__()

		self.vocab_size = vocab_size
		self.embeddings_dim = embeddings_dim
		self.hidden_size = hidden_size
		self.dropout = dropout
		self.num_classes = num_classes
		self.device = devie

		self._word_embedding = nn.Embedding(self.vocab_size,
											self.embedding_dim,
											padding_idx=0).requires_grad_(False)
		self._word_embedding.weight = nn.Parameter(embeddings)


		self._encoding = Seq2SeqEncoder(self.embedding_dim,
										self.hidden_size,
										bidirectional=True)

		self._attention = SoftmaxAttention()

		self._composition = Seq2SeqEncoder(self.hidden_size,
										   self.hidden_size,
										   bidirectional=True)

		self._projection = nn.Sequential(nn.Linear(4*2*self.hidden_size,
												   self.hidden_size),
										 nn.ReLU())

		self._composition = Seq2SeqEncoder(nn.LSTM,
										   self.hidden_size,
										   self.hidden_size,
										   bidirectional=True)
		
	def forward(self,
				premises,
				premises_lengths,
				hypotheses,
				hypotheses_lengths):
		"""
		Args:
			premises: A batch of varaible length sequences of word indices
				representing premises. The batch is assumed to be of size
				(batch, premises_length).
			premises_lengths: A 1D tensor containing the lengths of the
				premises in 'premises'.
			hypothesis: A batch of varaible length sequences of word indices
				representing hypotheses. The batch is assumed to be of size
				(batch, hypotheses_length).
			hypotheses_lengths: A 1D tensor containing the lengths of the
				hypotheses in 'hypotheses'.
		Returns:
			logits: A tensor of size (batch, num_classes) containing the
				logits for each output class of the model.
			probabilities: A tensor of size (batch, num_classes) containing
				the probabilities of each output class in the model.
		"""
		premises_mask = get_mask(premises, premises_lengths).to(self.device)
		hypotheses_mask = get_mask(hypotheses, hypotheses_lengths).to(self.device)

		embedded_premises = self._word_embedding(premises)
		embedded_hypotheses = self._word_embedding(hypotheses)
		
		encoded_premises = self._encoding(embedded_premises,
										  premises_lengths)
		encoded_hypotheses = self._encoding(embedded_hypotheses,
											hypotheses_lengths)


		attended_premises, attended_hypotheses =\
			self._attention(encoded_premises, premises_mask,
							encoded_hypotheses, hypotheses_mask)
			
		enhanced_premises = torch.cat([encoded_premises,
									   attended_premises,
									   encoded_premises - attended_premises,
									   encoded_premises * attended_premises],
									  dim=-1)
		 

		enhanced_hypotheses = torch.cat([encoded_hypotheses,
										 attended_hypotheses,
										 encoded_hypotheses - attended_hypotheses,
										 encoded_hypotheses * attended_hypotheses],
										dim=-1)

		projected_premises = self._projection(enhanced_premises)
		projected_hypotheses = self._projection(enhanced_hypotheses)

		v_ai = self._composition(projected_premises, premises_lengths)
		v_bj = self._composition(projected_hypotheses, hypotheses_lengths)






