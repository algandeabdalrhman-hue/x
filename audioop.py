# audioop.py — stub for python 3.14 (removed from stdlib)
# discord.py 2.0.0 imports this at startup but never actually calls it

def add(fragment1, fragment2, width):
	return b''

def adpcm2lin(data, state, width, stereo=False):
	return b'', None

def alaw2lin(data, width):
	return b''

def bias(fragment, width, bias):
	return b''

def cross(fragment, width):
	return 0

def findfactor(fragment, reference):
	return 1.0

def findfit(fragment, reference):
	return 0, 1.0

def findmax(fragment, length):
	return 0

def getsample(fragment, width, index):
	return 0

def lin2alaw(data, width):
	return b''

def lin2adpcm(data, state, width):
	return b'', None

def lin2lin(data, width, newwidth):
	return b''

def lin2ulaw(data, width):
	return b''

def max(fragment, width):
	return 0

def minmax(fragment, width):
	return 0, 0

def mul(fragment, width, factor):
	return b''

def ratecv(data, inrate, outrate, inwidth, outwidth, state, weightA=1, weightB=2):
	return b'', None

def reverse(fragment, width):
	return b''

def tomono(stereo, width):
	return b''

def tostereo(mono, width):
	return b''

def ulaw2lin(data, width):
	return b''
