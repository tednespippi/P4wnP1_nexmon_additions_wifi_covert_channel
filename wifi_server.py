#!/usr/bin/python
from __future__ import print_function


import logging
import sys
import time
import socket
import os
import Queue
from enum import Enum
from threading import Thread, Event
from select import select
from mame82_util import *

NETLINK_USERSOCK = 2
NETLINK_ADD_MEMBERSHIP = 1
SOL_NETLINK = 270

nlgroup = 21

# ToDo:
# - change manual string substitution for debug out to use the substitution of logging module
# - connection reset for invalid packets

logging.basicConfig(stream=sys.stderr, level=logging.INFO)

class Helper:
	@staticmethod
	def s2hex(s):
		#return "".join(map("0x%2.2x ".__mod__, map(ord, s)))
		return "".join(map("%2.2x".__mod__, map(ord, s)))
		
	@staticmethod
	def s2mac(s):
		s = Helper.s2hex(s)
		res = ""
		for i in range(0, 12, 2):
			res += s[i:i+2]
			if i < 10:
				res += ":"
		return res


class Packet:
	CTLM_TYPE_CON_INIT_REQ1 = 1
	CTLM_TYPE_CON_INIT_RSP1 = 2
	CTLM_TYPE_CON_INIT_REQ2 = 3
	CTLM_TYPE_CON_INIT_RSP2 = 4
	CTLM_TYPE_CON_RESET = 5
	
	PAY1_MAX_LEN = 28
	PAY2_MAX_LEN = 236
	
	# Data encoding
	#
	# SSID - 32 BYTES (pay1)
	# ----------------------
	# byte 0: pay1[0], if FlagControlMessage is set CTRL_TYPE
	# byte 1..27: pay1[0..27]
	# byte 28 ack_seq bits: 0..3 = ack, 4..7 = seq
	# byte 29 flag_len bits: 0 = FlagControlMessage, 1 = reserved, 2 = reserved, 3-7 = len_pay1
	# byte 30 clientID_srvID bits: 0..3 = clientID, 4..7 = srvID
	# byte 31 chk_pay1: 8 bit checksum
	#
	# Vendor Specific IE - 238 BYTES (pay2), could be missing
	# -----------------------------------------------------
	#
	# byte 0..235 pay2
	# byte 236 len_pay2: 
	# byte 237 chk_pay2: 8 bit checksum

	def __init__(self):
		self.sa = "" # 80211 SA
		self.da = "" # 80211 DA
		self.clientID = 0 # logical source (as we use scanning, on some devices the 802.11 SA could change and isn't reliable)
		self.srvID = 0 # logical destination
		self.pay1 =  "" # encoded in SSID
		self.pay2 = None # encoded in vendor IE (optional, only if possible)
		self.seq = 0
		self.ack = 0
		self.FlagControlMessage = False # If set, the payload contains a control message, pay1[0] is control message type
		self.ctlm_type = 0

	@staticmethod
	def parse2packet(sa, da, raw_ssid_data, raw_ven_ie_data=None):
		packet = Packet()
	
		packet.sa = sa
		packet.da = da
		
		if raw_ven_ie_data != None:
			pay2_len = ord(raw_ven_ie_data[236])
			packet.pay2 = raw_ven_ie_data[:pay2_len]
		
		ack_seq = ord(raw_ssid_data[28])
		packet.ack = ack_seq >> 4
		packet.seq = ack_seq & 0x0F
	
        	flag_len = ord(raw_ssid_data[29])
		packet.FlagControlMessage = (flag_len & 0x80) != 0
		if packet.FlagControlMessage:
			packet.ctlm_type = ord(raw_ssid_data[0])
		pay1_len = flag_len & 0x1F
		packet.pay1 = raw_ssid_data[:pay1_len]
		
		clientID_srvID = ord(raw_ssid_data[30])
		packet.clientID = clientID_srvID >> 4
		packet.srvID = clientID_srvID & 0x0F
		
		return packet
	
	def generateRawSsid(self, with_TL=True):
		payload = self.pay1[:28] # truncate, ToDo: warn if payload too large
		if self.FlagControlMessage:
			payload = chr(self.ctlm_type) + self.pay1[1:28]
		pay_len = len(payload)
		out = payload + (28 - pay_len) * "\x00" # pad with zeroes
		
		# ack_seq
		ack_seq = (self.ack << 4) | (self.seq & 0x0F)
		out += chr(ack_seq)
		
		# flag_len
		flag_len = pay_len
		if self.FlagControlMessage:
			flag_len += 0x80
		out += chr(flag_len)
		
	
		# clientID_srvID
		clientID_srvID = (self.clientID << 4) | (self.srvID & 0x0F)
		out += chr(clientID_srvID)
		
		# chksum
		chk = Packet.simpleChecksum8(out)
		out += chr(chk)
		
		if with_TL:
			out = "\x00\x20" + out
		return out
	
	def generateRawVenIe(self, with_TL=True):
		if self.pay2 == None:
			return None

		payload = self.pay2[:236] # truncate, ToDo: warn if payload too large
		pay_len = len(payload)
		out = payload + (236 - pay_len) * "\x00" # pad with zeroes

		# build len_id octet
		length = len(payload)
		out += chr(length)

		# calculate checksum
		chk = Packet.simpleChecksum8(out)
		out += chr(chk)

		if with_TL:
			# add SSID type and length
			out = "\xDD\xEE" + out # type 221, length 238

		return out
	
	@staticmethod
	def checkLengthChecksum(raw_ssid_data, raw_ven_ie_data=None):	
		if len(raw_ssid_data) != 32:
			return False
		if ord(raw_ssid_data[31]) != Packet.simpleChecksum8(raw_ssid_data, 31):
			return False
		if raw_ven_ie_data != None:
			if len(raw_ven_ie_data) != 238:
				return False
			if ord(raw_ven_ie_data[237]) != Packet.simpleChecksum8(raw_ven_ie_data, 237):
				return False
		return True
	
	def print_out(self):
		logging.debug("Packet")
		logging.debug("\tSA:\t{0}".format(self.sa))
		logging.debug("\tDA:\t{0}".format(self.da))
		logging.debug("\tClientID:\t{0}".format(self.clientID))
		logging.debug("\tsrvID:\t{0}".format(self.srvID))
		
		logging.debug("\tSSID payload len:\t{0}".format(len(self.pay1)))
		logging.debug("\tSSID payload:\t{0}".format(Helper.s2hex(self.pay1)))
		if self.pay2 == None:
			logging.debug("\tVendor IE:\tNone")
		else:
			logging.debug("\tVendor IE payload len:\t{0}".format(len(self.pay2)))
			logging.debug("\tVendor IE payload:\t{0}".format(Helper.s2hex(self.pay2)))
		logging.debug("\tFlag Control Message:\t{0}".format(self.FlagControlMessage))
		if self.FlagControlMessage:
			logging.debug("\tCTLM_TYPE:\t{0}".format(self.ctlm_type))
		logging.debug("\tSEQ:\t{0}".format(self.seq))
		logging.debug("\tACK:\t{0}".format(self.ack))
	
	@staticmethod
	def simpleChecksum16(input, len_to_include=-1):
		sum = 0
		if len_to_include == -1:
			len_to_include = len(input)

		for off in range(len_to_include):
			sum += ord(input[off])
			sum %= 0xFFFF

		sum = ~sum

		return [(sum >> 8) & 0xFF, sum & 0xFF]
	

	@staticmethod
	def simpleChecksum8(input, len_to_include=-1):
		sum = 0
		if len_to_include == -1:
			len_to_include = len(input)

		for off in range(len_to_include):
			sum += ord(input[off])
			sum &= 0xFF

		sum = ~sum

		return sum & 0xFF
	

class ConnectionQueue:
	def __init__(self, max_connections=15):
		self.__available_client_IDs = []
		self.__queued_connections = []
		self.__wait_accept_state_change = Event() # is triggered, when a connection changes to pending_accept or from pending_accept to another state
		self.__wait_accept_state_change.clear()
		for ID in range(max_connections):
			self.__available_client_IDs.insert(0, ID+1)
		self.max_connections = max_connections
		
	def __handleConnectionStateChange(self, con, oldstate, newstate):
		logging.debug("Connection clientID {0}, old state: {1}, new state {2}".format(con.clientID, oldstate, newstate))
		
		if newstate == ClientSocket.STATE_PENDING_ACCEPT or oldstate == ClientSocket.STATE_PENDING_ACCEPT:
			# Trigger event when a connection enters or leaves pending_accept state
			self.__wait_accept_state_change.set()
			
	def waitForPendingAcceptStateChange(self):
		while not self.__wait_accept_state_change.isSet():
			# interrupted passive wait (interrupt to allow killing thread)
			self.__wait_accept_state_change.set()
			
		self.__wait_accept_state_change.clear()
		
	def provideNewClientSocket(self, srvID):
		try:
			clientID = self.__available_client_IDs.pop()
		except IndexError:
			# no more client IDs left
			return None
		newcon = ClientSocket(srvID, self.__handleConnectionStateChange)
		newcon.clientID = clientID
		
		# add to internal queue data
		self.__queued_connections.append(newcon)
		
		# return none if no new client is available
		return newcon
	
	def getConnectionListByState(self, con_state):
		res = []
		for con in self.__queued_connections:
			if con.state == con_state:
				res.append(con)
		return res

	def getConnectionByClientIV(self, clientIV):
		res = None
		for con in self.__queued_connections:
			if con.clientIV == clientIV:
				res = con
				break
		return res
	
	def getConnectionByClientID(self, clientID):
		res = None
		for con in self.__queued_connections:
			if con.clientID == clientID:
				res = con
				break
		return res		
		

class ClientSocket(object):
	MTU_WITH_VEN_IE = 28 + 236 # 28 bytes netto SSID payload + 236 bytes netto vendor ie payload
	MTU_WITHOUT_VEN_IE = 28
	
	STATE_CLOSE = 1 # communication possible
	STATE_PENDING_OPEN = 2 # connection init started but not done
	STATE_PENDING_ACCEPT = 3 # connection init done, but connection not accepted
	STATE_OPEN = 4 # connection be used for communication
	STATE_PENDING_CLOSE = 5 # connection is being transfered to close state
	STATE_DELETE = 6 # connection is ready to be deleted
	
	def __init__(self, srvID, stateChangeCallback=None):
		self.stateChangeCallback = stateChangeCallback
		self.__state = ClientSocket.STATE_CLOSE
		self.srvID = srvID
		self.clientID = 0 # client ID in use for this connection		
		self.clientIV = 0 # random 32 bit IV used by the client during connection_init
		self.clientIVBytes = None # random 32 bit IV used by the client during connection_init
		self.clientSA = None # Source address used by client IN FIRST CONNECT (could change during scans and isn't updated)
		self.txVenIeAllowed = False # if true vendor IE could be used when transmitting to client
		self.rxVenIePossible = False # if true vendor IE could be received from client
		self.mtu = ClientSocket.MTU_WITH_VEN_IE # mtu (depending on txVenIeAllowed)
		self.last_rx_packet = None
		self.tx_packet = None
		self.clientSocket = None
		self.__in_queue = Queue.Queue()
		self.__out_queue = Queue.Queue()

	@property
	def state(self):
		return self.__state
	
	@state.setter
	def state(self, value):
		# this setter could be used to assure valid state transfers
		oldstate = self.__state
		if value == oldstate:
			# no state transfer
			return
		self.__state = value
		if self.stateChangeCallback != None:
			self.stateChangeCallback(self, oldstate, value)
	
	def shutdown(self):
		# send reset
		# change state to close
		pass

	def read(self, bufsize, block=False):
		if self.state != ClientSocket.STATE_OPEN:
			return ""
		
		if not self.hasInData():
			return ""
		
		len_received = 0
		len_chunk = 0
		current_chunk = None
		buf = ""
		
		while len_received <  bufsize:
			if self.__in_queue.qsize() == 0:
				break # stop if no more data in inqueue
			
			# Caution: This isn't thread safe, as a new element could be put() on the queue by the input_handler AFTER WE STORED len_chunk
			# ToDo: put a thread LOCK on queue before length check
			len_chunk = len(self.__in_queue.queue[0])
			
			if (len_chunk + len_received) >  bufsize:
				break # abort, as we would exceed the gicen buffer size (before popping from queue)
			
			current_chunk = self.__in_queue.get()
			
			if len(current_chunk) == 0:
				# break after popping from queue
				break # zero len payload indicates EOF, has to be replaced by dedicated CTLM_TYPE (no priority, arriving in order)
			
			buf += current_chunk
			len_received += len(current_chunk)
		
		return buf

	# note: block parameter is currently always assume to be True
	def send(self, string, block=True):
		for off in range(0, len(string), self.mtu):
			chunk = string[off:off+self.mtu]
			self.__pushOutboundData(chunk)

	def __pushOutboundData(self, data, block=True):
		logging.debug("Pushing outdata {0}".format(data))
		self.__out_queue.put(data, block=block)

	def __popInboundData(self):
		if self.hasInData():
			return self.__in_queue.get()
		else:
			return ""
		
	def hasInData(self):
		# type: () -> bool
		return self.__in_queue.qsize() > 0
	
	def handleRequest(self, req):
		# type: (Packet) -> Packet
		
		#### CTLM handling ######
		if req.ctlm_type == Packet.CTLM_TYPE_CON_INIT_REQ1:
			# first con_init_req1 as socket is still in close state
			if self.state == ClientSocket.STATE_CLOSE:				
				self.clientSA = req.sa # not usable as identifier for client !!!COULD CHANGE!!!
			
				# generate response
				resp = Packet()
				resp.da = req.sa # direct probe response, even if SA changes
				resp.pay1 = chr(Packet.CTLM_TYPE_CON_INIT_RSP1) + self.clientIVBytes
				resp.pay2 = self.clientIVBytes
				resp.FlagControlMessage = True
				resp.ctlm_type = Packet.CTLM_TYPE_CON_INIT_RSP1
				resp.seq = 1
				# if we received a vendor IE, we inform the client by appending 0x02 at resp.pay1[5]
				# if we aren't able to receive the vendor IE, we inform the client by appending 0x01 at resp.pay1[5]
				if req.pay2 != None:
					resp.pay1 += chr(2)
					self.rxVenIePossible = True
				else:
					resp.pay1 += chr(1)
					self.rxVenIePossible = False
				# we hand out a new clientID to the pending (not yet established) connection
				resp.clientID = self.clientID
				resp.srvID = self.srvID
				resp.ack = req.seq
			
				self.tx_packet = resp
				self.last_rx_packet = req
				resp.print_out()
			
			
				# transfer state of the connection
				self.state = ClientSocket.STATE_PENDING_OPEN
				
				return self.tx_packet
			
			# repeated con_init_req1 as socket is already in pending_open state
			elif self.state == ClientSocket.STATE_PENDING_OPEN and req.ack == 0:
				logging.debug("Stage 1 init request of this client already added, sending stored response ...")
				return self.tx_packet
			else:
				printf("Invalid socket state {0} for CTLM_TYPE_CON_INIT_REQ1".format(self.state))
				# ToDo: send reset
				return None
		elif req.seq == 2 and req.ctlm_type == Packet.CTLM_TYPE_CON_INIT_REQ2:
			if self.state == ClientSocket.STATE_PENDING_OPEN:
				print("InReq2 from ClientID {0} ...".format(req.clientID))
			
				resp = self.tx_packet # fetch old response
				resp.ack = req.seq
				resp.seq = 2
				resp.ctlm_type = Packet.CTLM_TYPE_CON_INIT_RSP2
				resp.pay1 = chr(Packet.CTLM_TYPE_CON_INIT_RSP2) + self.clientIVBytes
				self.last_rx_packet = req
				self.tx_packet = resp
			
				if ord(req.pay1[5]) == 2:
					# client received vendor IE in response1
					self.txVenIeAllowed = True
					self.mtu = ClientSocket.MTU_WITH_VEN_IE
				elif ord(req.pay1[5]) == 1:
					# client didn't receive vendor IE from response1
					self.txVenIeAllowed = False
					self.mtu = ClientSocket.MTU_WITHOUT_VEN_IE
				else:
					# req.pay1[5] invalid --> packet invalid
					logging.debug("Received invalid information for ven IE receive caps from clientID {0}, dropped...",  req.clientID)
					return None
				
				
				# Handover to accept() method !!
				self.state = ClientSocket.STATE_PENDING_ACCEPT # done by event emitter in setter of state
				
				print("... InRsp2: Client added to accept-queue.")
				self.print_out()				
				
				return self.tx_packet
			
				
			elif self.state == ClientSocket.STATE_PENDING_ACCEPT:
				logging.debug("Connection in handover queue, resending stage2 response")
				return self.tx_packet
			elif self.state == ClientSocket.STATE_OPEN and self.last_rx_packet.ctlm_type == Packet.CTLM_TYPE_CON_INIT_REQ2:
				logging.debug("Resending stage2 response")
				return self.tx_packet			
			else:
				logging.debug("Invalid socket state {0} for CTLM_TYPE_CON_INIT_REQ2".format(self.state))
				# ToDo: send reset
				return None				
		
		#### data handling ######
		
		# rx.ack		rx.seq			action
		# == tx.seq	!= last_rx.seq+1	--> tx.seq+=1, tx.ack = last_tx.ack, pack new outdata into resp payload
		# == tx.seq	== last_rx.seq+1	--> tx.seq+=1, tx.ack = rx.seq, pack new outdata into resp payload, put indata into input queue, update last_rx_packet
		# != tx.seq	!= last_rx.seq+1	--> tx.seq = last_tx.seq, tx.ack = last_tx.ack , resend last_tx_packet
		# != tx.seq	== last_rx.seq+1	--> tx.seq = last_tx.seq, tx.ack = rx.seq, put indata into input queue, update last_rx_packet
		
		# Note on flow control: PingPong, no slinding window, although it'd be usefull for bulk probe responses ... anyway, this is only a PoC
		
		if not req.FlagControlMessage:
			if self.state != ClientSocket.STATE_OPEN:
				logging.debug("Ignored inbound data packet, as socket for client ID {0} isn't in OPEN state".format(self.clientID))
				return None
			# assure tx packet isn't CTLM
			self.tx_packet.FlagControlMessage = False
			
			# check if seq has advanced
			if req.seq == ((self.last_rx_packet.seq + 1) & 0x0F):
				# new input packet, push data to in_queue
				indata = req.pay1
				if req.pay2 != None:
					indata += req.pay2
				self.__in_queue.put(indata)
				logging.debug("Enqueueing indata (client {0}): '{1}'".format(self.clientID,  indata))
				
				# update last packet
				self.last_rx_packet = req
				
				# update tx ack
				self.tx_packet.ack = req.seq
			
			# check if ack is fitting last transmitted seq, thus we could push a new outbound packet
			if req.ack == self.tx_packet.seq:
				# advance tx seq
				self.tx_packet.seq += 1
				self.tx_packet.seq &= 0x0F # modulo 16
								
				# pop data from out_queue and update payload NOTE: data from queue should always be <= self.mtu
				outdata = ""
				if self.__out_queue.qsize() > 0:
					outdata = self.__out_queue.get()
				
				logging.debug("sending outdata: {0}".format(outdata))
					
				# THIS SHOULD NEVER HAPPEN
				if len(outdata) > self.mtu:
					logging.debug("Error: Outdata has been truncate, because it was larger than MTU")
					outdata = outdata[:self.mtu]
				
				self.tx_packet.pay1 = outdata[:Packet.PAY1_MAX_LEN]	
				if len(outdata) > Packet.PAY1_MAX_LEN:
					self.tx_packet.pay2 = outdata[Packet.PAY1_MAX_LEN:]
				else:
					self.tx_packet.pay2 = None
				
				
			return self.tx_packet
				
			
			

	def print_out(self):
		logging.debug("Connection")
		logging.debug("\tClientID:\t{0}".format(self.clientID))
		logging.debug("\tClientIV bytes:\t{0}".format(Helper.s2hex(self.clientIVBytes)))
		logging.debug("\tClientSA:\t{0}".format(self.clientSA))
		logging.debug("\tTX vendor IE possible:\t{0}".format(self.txVenIeAllowed))
		logging.debug("\tRX vendor IE possible:\t{0}".format(self.rxVenIePossible))
		logging.debug("\tTX MTU:\t{0}".format(self.mtu))

			
class ServerSocket:
	MAX_CONNECTIONS_LIMIT = 7 # more clients aren't allowe
	__global_firmware_event_queue = None
	__global_firmware_event_thread = None
	__nl_in_socket = None
	__nl_out_socket = None
	__nl_out_socket_fd = None
	__nl_thread_stop = Event()
	
	
	def __init__(self):
		self.nl_out_socket = None
		
		self.__connection_queue = None
		
		self.srvID = 7 # identifies the server (could be seen as IP, possible values 1..15)
		self.max_connections = 7
		self.isBound = False
		self.isListening = False
		
	@staticmethod
	def eprint(message):
		sys.stderr.write("WiFiSocket ERROR: "+message + "\n")

	@staticmethod
	def __parse_ies(s):
		res = {}
		if len(s) < 2:
			return res
		pos = 0	
		while pos < (len(s)-2):
			t = ord(s[pos])
			pos+=1
			l = ord(s[pos])
			pos+=1
			v = s[pos:pos+l]
			pos += l
			res.update({t: [l, v]})
		
		return res
		
		
	def bind(self, srvID=7):
		self.srvID = srvID
		
		if ServerSocket.__global_firmware_event_queue == None:
			ServerSocket.__global_firmware_event_queue = Queue.Queue()
		
		if not ServerSocket.__nl_in_socket == None:
			ServerSocket.eprint("bind() netlink multicast listener already running...")
			self.isBound = True
			return None

		# open socket to receive multicast message from firmware
		#########################################################
		try:
			s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_USERSOCK)
		except socket.error:
			ServerSocket.eprint("Error creating netlink socket for Firmware multicasts")
			return None

		# bind to kernel
		s.bind((os.getpid(), 0))
		
		# 270 is SOL_NETLINK and 1 is NETLINK_ADD_MEMBERSHIP
		try:
			s.setsockopt(SOL_NETLINK, NETLINK_ADD_MEMBERSHIP, nlgroup)
		except socket.error:
			ServerSocket.eprint("Failed to attach to netlink multicast group {0}, try with root permissions".format(nlgroup))
			return None
		
		ServerSocket.__nl_in_socket = s
		
		# open socket for unicat messages to firmware #
		###############################################
		s = nexconf.openNL_sock()
		ServerSocket.__nl_out_socket = s # socket
		ServerSocket.__nl_out_socket_fd = os.fdopen(s.fileno(), 'w+b') # writable FD

		print("Bound to server ID {0}".format(self.srvID))
		
		
		
		self.isBound = True
		
	def unbind(self):
		# stop event listener thread for Kernel NL multicasts
		logging.debug("Stop listening for firmware events...")
		self.__nl_thread_stop.set()
		logging.debug("Unregistering firmware event listener")
		ServerSocket.__nl_in_socket.close()
		ServerSocket.__nl_out_socket_fd.close()
		ServerSocket.__nl_out_socket.close()
		self.isListening = False
		self.isBound = False
		
			
	def listen(self, max_connections=7):
		if max_connections > ServerSocket.MAX_CONNECTIONS_LIMIT:
			ServerSocket.eprint("Max connections limited to {0}, but {1} given on listen()".format(ServerSocket.MAX_CONNECTIONS_LIMIT, max_connections))
			return
		if not self.isBound:
			ServerSocket.eprint("Socket isn't bound, listening not possible. Call bind() first.")
			return
		self.max_connections = max_connections
		self.__connection_queue = ConnectionQueue(max_connections)
				
		
		# start Thread which handles incoming probe events
		ServerSocket.__global_firmware_event_thread = Thread(target = self.__firmware_event_reader, name = "WiFiSocket Firmware event thread", args = ( ))
		ServerSocket.__global_firmware_event_thread.start()
		
		self.isListening = True
		print("Listening for incoming connections (max {0})".format(max_connections))
	
	def __firmware_event_reader(self):
		logging.debug("Listening for WiFi firmware events")
		sfd = ServerSocket.__nl_in_socket.fileno()
		
		while not ServerSocket.__nl_thread_stop.isSet():
		
			# instead of blocking read, we poll the socket (blocking, but with timeout)
			# this is used to keep the thread responsive in order to allow ending it (at least with a delay of read_timeout)
			read_timeout = 0.5
			sel = select([sfd], [], [], read_timeout) # test if readable data arrived on nl_socket, interrupt after timeout
			if len(sel[0]) == 0:
				# no data arrived
#				print "No data"
				continue
			
			data = ServerSocket.__nl_in_socket.recvfrom(0xFFFF)[0]
			
			# parse data
			data = data[16:] # strip off nlmsghdr (16)
			f80211_fc_type_subtype = data[0] # store FC
			if f80211_fc_type_subtype != "\x40":
				logging.debug("Firmware event received, but frame isn't a mgmt probe request")
				continue
			f80211_fc_flags = data[1] # store flags
			f80211_duration = data[2:4] # store duration
			f80211_da = data[4:10] # store destinatioon address
			f80211_sa = data[10:16] # store source address
			f80211_bssid = data[16:22] # store bssid
			f80211_fragment = data[22:24] # store fragment
			f80211_parameters = data[24:] # store additional IEs (TLV list)
			f80211_parameters = f80211_parameters[:-2] # fix to avoid parsing 0x0000 padding as SSID type
			
			ies = ServerSocket.__parse_ies(f80211_parameters)
			
			# check fo SSID
			ssid = None
			ssid_len = 0
			if 0 in ies:
				ssid_len = ies[0][0]
				ssid = ies[0][1]
			else:
				continue
			
			
			# check for vendor specific IE (we only check one of the possible vendor IEs)
			ven_ie = None
			ven_ie_len = 0
			if 221 in ies:
				ven_ie_len = ies[221][0]
				ven_ie = ies[221][1]
				
			
			if not Packet.checkLengthChecksum(ssid,  ven_ie):
				#logging.debug("Packet dropped because length or checksum are wrong")
				continue
							

			
			# create a packet and dispatch it
			packet = Packet.parse2packet(Helper.s2mac(f80211_sa), Helper.s2mac(f80211_da), ssid, ven_ie)
			self.__inbound_dispatcher(packet)
			
			
		logging.debug("... stopped listening for firmware events")
	
	@staticmethod
	def __send_probe_resp_to_driver(sa, da, ie_ssid_data, ie_vendor_data=None):
		if ServerSocket.__nl_out_socket_fd == None:
			ServerSocket.eprint("Socket FD for unicast to device driver not defined")
			return
	
		arr_bssid = mac2bstr(sa)
		arr_da = mac2bstr(da)
		
		ie_ssid_type = 0
		ie_ssid_len = 32
		ie_vendor_type = 221
		ie_vendor_len = 238
		
		buf = ""
		
		if ie_vendor_data == None:
			buf = struct.pack("<II6s6sBB32s", 
				MaMe82_IO.MAME82_IOCTL_ARG_TYPE_SEND_PROBE_RESP, 
				48, # 6 + 6 + 1 + 1 +32 + 1 + 1 + 238
				arr_da, 
				arr_bssid,
				ie_ssid_type,
				ie_ssid_len,
				ie_ssid_data)
		else:
			buf = struct.pack("<II6s6sBB32sBB238s", 
				MaMe82_IO.MAME82_IOCTL_ARG_TYPE_SEND_PROBE_RESP, 
				286, # 6 + 6 + 1 + 1 +32 + 1 + 1 + 238
				arr_da, 
				arr_bssid,
				ie_ssid_type,
				ie_ssid_len,
				ie_ssid_data,
				# insert additional IEs here
				ie_vendor_type,
				ie_vendor_len,
				ie_vendor_data)
		
		#print repr(buf)
		
		ioctl_sendprbrsp = nexconf.create_cmd_ioctl(MaMe82_IO.CMD, buf, True)
		nexconf.sendNL_IOCTL(ioctl_sendprbrsp, nl_socket_fd=ServerSocket.__nl_out_socket_fd)
	
	tmp = 0
	def __inbound_dispatcher(self, req):
		#logging.debug("Inbound dispatcher received packet")
		
		
		# "init connection" set
		if req.FlagControlMessage and self.isListening:
			if req.ctlm_type == Packet.CTLM_TYPE_CON_INIT_REQ1 or req.ctlm_type == Packet.CTLM_TYPE_CON_INIT_REQ2:
				if req.srvID != self.srvID:
					logging.debug("Control message CTLM_TYPE {0} targets srvID {1}, but we're {2} ... packet dropped".format(req.ctlm_type, req.srvID, self.srvID))
				else:
					self.handle_request(req)
			else:
				logging.debug("Unhandled CTLM_TYPE {0} !! Dropped packet in dispatcher!!".format(req.ctlm_type))
		elif self.isListening:
			# no CTLM, but data
			self.handle_request(req)
		else:
			# don't handle frame (no probe responses)
			logging.debug("!! Dropped packet in dispatcher!!")
			req.print_out()
			
		

	def sendResponse(self, resp):
		if len(resp.sa) == 0:
			resp.sa = "de:ad:be:ef:13:37" # ToDo: randomize bssid/sa
		ServerSocket.__send_probe_resp_to_driver(resp.sa, resp.da, resp.generateRawSsid(False), resp.generateRawVenIe(False))
	
	def handle_request(self, req):
		# ToDo: this method handles everything, thus code should be moved to inbound dispatcher
		#logging.debug("Init connection")
		
		q = self.__connection_queue
		
		# check if stage1 connection init request (SSID_payload with random 4 byte IV, clientId 0, seq 1)
		########################################
		# - pending stage1 requests are handled in __connection_queue_stage1
		# - as soon as a corresponding and valid stage2 request is received, the client is moved over
		# to accept queue (represented by a new socket and assigned clientId)
		# - the stage2 response is sent by the accept() method
		# - the stage1 response is sent by the listen method 
		if req.ctlm_type == Packet.CTLM_TYPE_CON_INIT_REQ1 and req.seq == 1:
			# the very first connection (Packet.CTLM_TYPE_CON_INIT_REQ1) request couldn't be handled by a client socket, as no one does exist
			# this is only true for the first probe request of this kind (we get them in bulks, with repetitions)
			
			# extract init vector
			iv = struct.unpack("I", req.pay1[1:5])[0]
			
			
			con_pending_open = q.getConnectionByClientIV(iv)
			if con_pending_open == None: # no ClientSocket exists for this IV
				print("InReq1: Connection request from client IV: {0}".format(iv))
				req.print_out()
				
				cl_sock = q.provideNewClientSocket(self.srvID)
				if cl_sock == None:
					logging.debug("No additional connections possible")
					# no need to send a connection reset, as the client is still in initial state and continues trying to connect
					return
				
				cl_sock.clientIV = iv
				cl_sock.clientIVBytes = req.pay1[1:5]
				
				resp = cl_sock.handleRequest(req)
				print("... InRsp1: Handing out client ID {0}".format(resp.clientID))
				self.sendResponse(resp)
			# ClientSocket for given IV exists already
			else:
				resp = con_pending_open.handleRequest(req)
				if resp != None:
					self.sendResponse(resp)
				else:
					logging.debug("unhandled request")
					req.print_out				
				#logging.debug("Received continuos stage1 request for socket which is not in pending_open state") # shouldn't happen (only if IV is reused)
				# ToDo: send reset
		else:
			cl_sock = q.getConnectionByClientID(req.clientID)
			if cl_sock != None:
				resp = cl_sock.handleRequest(req)
				if resp != None:
					self.sendResponse(resp)
				else:
					logging.debug("Clientsocket has no response for following request")
					req.print_out()
			else:
				logging.debug("No target socket for following request")
				req.print_out()
				
		
	
	def accept(self):
		# ask connection queue for the first connection in Connection.STATE_PENDING_ACCEPT
		# if there's no connection, passive wait for a state change
		# repeat till there's a connection in state Connection.STATE_PENDING_ACCEPT and return it
		# before returning, set state to open
		
		logging.debug("Entering accept()")
		
		while True:
			cons_pa = self.__connection_queue.getConnectionListByState(ClientSocket.STATE_PENDING_ACCEPT)
			if len(cons_pa) == 0:
				# no pending connection, passive wait and retry
				self.__connection_queue.waitForPendingAcceptStateChange()
				continue
			else:
				# pending connection found
				result_con = cons_pa[0]
				
				result_con.state = ClientSocket.STATE_OPEN
				logging.debug("...returning from accept")
				return result_con
		
		

import cmd	
class ClientShell(cmd.Cmd):
	def __init__(self, client_socket):
		# type (ClientSocket)
		self.csock = client_socket
		self.prompt = "WiFi Shell >"
		cmd.Cmd.__init__(self)
	
	def emptyline(self):
		pass # don't repeat last line
	
	def do_test(self, line):
		# print(line)
		interact = True
		while interact:
			try:
				if select([sys.stdin], [], [], 0.05)[0]: # 50 ms timeout, to keep CPU load low
					input = sys.stdin.readline() # replace readline by accumulation of input chars til carriage return
					input = input.replace('\n', '\r\n')
					print(input)
					self.csock.send(input)
		
			except KeyboardInterrupt:
				print("\nInteraction stopped by keyboard interrupt.\nTo continue interaction use 'interact'.")
				interact = False
			while self.csock.hasInData():
				inchunk = self.csock.read(self.csock.mtu)
				if len(inchunk) > 0:
					print("inchunk: {0}".format(inchunk))
				else:
					logging.debug("Empty packet")
	
	
##### MAIN CODE #####
SERVER_ID = 9
serv_socket = ServerSocket()
serv_socket.bind(SERVER_ID)
serv_socket.listen(7)
try:
	while True:
		con = serv_socket.accept()
		logging.debug("accepted connection:")
		con.print_out()
		shell = ClientShell(con)
		shell.cmdloop()
		
		# we directly interact with the first connection, till a connection handler is implemented
		#time.sleep(1)
finally:
	serv_socket.unbind()	
	
	