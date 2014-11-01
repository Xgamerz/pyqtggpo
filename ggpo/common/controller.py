# -*- coding: utf-8 -*-
import cgi
import os
import re
import socket
import time
import struct
import select
import errno
from random import randint
from subprocess import Popen
from PyQt4 import QtCore
from ggpo.common.runtime import *
from ggpo.common.geolookup import geolookup, isUnknownCountryCode
from ggpo.common.player import Player
from ggpo.common.playerstate import PlayerStates
from ggpo.common.protocol import Protocol
from ggpo.common.settings import Settings
from ggpo.common.unsupportedsavestates import readLocalJsonDigest
from ggpo.common.util import findFba, logdebug, loguser, packagePathJoin, findUnsupportedGamesavesDir, sha256digest
from ggpo.gui.colortheme import ColorTheme


class Controller(QtCore.QObject):
    sigActionFailed = QtCore.pyqtSignal(str)
    sigChallengeCancelled = QtCore.pyqtSignal(str)
    sigChallengeDeclined = QtCore.pyqtSignal(str)
    sigChallengeReceived = QtCore.pyqtSignal(str)
    sigChannelJoined = QtCore.pyqtSignal()
    sigChannelsLoaded = QtCore.pyqtSignal()
    sigChatReceived = QtCore.pyqtSignal(str, str)
    sigIgnoreAdded = QtCore.pyqtSignal(str)
    sigIgnoreRemoved = QtCore.pyqtSignal(str)
    sigLoginFailed = QtCore.pyqtSignal()
    sigLoginSuccess = QtCore.pyqtSignal()
    sigMotdReceived = QtCore.pyqtSignal(str, str, str)
    sigNewVersionAvailable = QtCore.pyqtSignal(str, str)
    sigPlayerNewlyJoined = QtCore.pyqtSignal(str)
    sigPlayerStateChange = QtCore.pyqtSignal(str, int)
    sigPlayersLoaded = QtCore.pyqtSignal()
    sigServerDisconnected = QtCore.pyqtSignal()
    sigStatusMessage = QtCore.pyqtSignal(str)

    (STATE_TCP_READ_LEN, STATE_TCP_READ_DATA) = range(2)

    def __del__(self):
        # noinspection PyBroadException
        try:
            self.tcpSock.close()
            self.udpSock.close()
        except:
            pass

    def __init__(self):
        super(Controller, self).__init__()
        self.selectTimeout = 1
        self.sequence = 0x1
        self.tcpSock = None
        self.tcpConnected = False
        self.tcpData = ''
        self.tcpReadState = self.STATE_TCP_READ_LEN
        self.tcpResponseLen = 0
        self.tcpCommandsWaitingForResponse = dict()
        self.udpSock = None
        self.udpConnected = False
        self.selectLoopRunning = True

        self.username = ''
        self.channel = 'lobby'
        self.rom = ''
        self.fba = None
        self.checkInstallation()
        self.unsupportedRom = ''
        self.checkUnsupportedRom()
        self.playingagainst = ''

        self.challengers = set()
        self.challenged = None
        self.channels = {}
        self.pinglist = {}
        self.players = {}
        self.available = {}
        self.playing = {}
        self.awayfromkb = {}
        self.ignored = Settings.pythonValue(Settings.IGNORED) or set()
        self.sigStatusMessage.connect(logdebug().info)

    def addIgnore(self, name):
        if name != self.username:
            self.ignored.add(name)
            self.saveIgnored()
            self.sigIgnoreAdded.emit(name)

    def addUser(self, **kwargs):
        if 'player' in kwargs:
            name = kwargs['player']
            if name not in self.available and name not in self.awayfromkb and name not in self.playing:
                self.sigPlayerNewlyJoined.emit(name)
            if name in self.players:
                p = self.players[name]
                for k, v in kwargs.items():
                    if v and not (k == 'cc' and isUnknownCountryCode(v)):
                        setattr(p, k, v)
            else:
                p = Player(**kwargs)
                self.players[name] = p
                self.sendPingQuery(p)
                if isUnknownCountryCode(p.cc):
                    p.cc, p.country, p.city = geolookup(p.ip)

    def checkInstallation(self):
        fba = Settings.value(Settings.GGPOFBA_LOCATION)
        if fba and os.path.isfile(fba):
            self.fba = os.path.abspath(fba)
        if self.fba:
            return True
        else:
            msg = ''
            if not self.fba:
                msg += "ggpo installation not found\n"
            self.sigStatusMessage.emit(msg)
            return False

    def checkRom(self):
        if self.channel == 'unsupported':
            return True
        if self.channel and self.channel != "lobby":
            romdir=Settings.value(Settings.ROMS_DIR)
            if romdir:
                rom = os.path.join(romdir, "{}.zip".format(self.rom))
                if os.path.isfile(rom):
                    return True
            rom = self.ggpoPathJoin("ROMs", "{}.zip".format(self.rom))
            if os.path.isfile(rom):
                return True
            else:
                self.sigStatusMessage.emit('Warning: {} not found. Required to play or spectate.'.format(rom))
                self.sigStatusMessage.emit("Please close emulator and configure Setting > Locate ROMs folder")
        return False

    def checkUnsupportedRom(self):
        if self.fba:
            d = findUnsupportedGamesavesDir()
            if d:
                unsupported = os.path.join(os.path.dirname(self.fba), 'savestates', 'unsupported_ggpo.fs')
                if os.path.isfile(unsupported):
                    unsupported = sha256digest(unsupported)
                    localJsonDigest = readLocalJsonDigest()
                    for k, v in localJsonDigest.items():
                        if v == unsupported:
                            self.unsupportedRom = os.path.splitext(k)[0]
                            break

    def connectTcp(self):
        self.tcpConnected = False
        #noinspection PyBroadException
        try:
            if self.tcpSock:
                self.tcpSock.close()
            self.tcpSock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcpSock.connect(('ggpo-ng.com', 7000,))
            self.tcpConnected = True
        except Exception:
            self.sigStatusMessage.emit("Cannot connect to GGPO-NG server")
            self.sigServerDisconnected.emit()
        return self.tcpConnected

    def connectUdp(self):
        self.udpConnected = False
        try:
            if self.udpSock:
                self.udpSock.close()
            self.udpSock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udpSock.bind(('0.0.0.0', 6009,))
            self.udpConnected = True
        except socket.error:
            self.sigStatusMessage.emit("Cannot bind to port udp/6009")
        return self.udpConnected

    def dispatch(self, seq, data):
        logdebug().info('Dispatch ' + Protocol.outOfBandCodeToString(seq) + ' ' + repr(data))
        # out of band data
        if seq == Protocol.CHAT_DATA:
            self.parseChatResponse(data)
        elif seq == Protocol.PLAYER_STATE_CHANGE:
            self.parseStateChangesResponse(data)
        elif seq == Protocol.CHALLENGE_DECLINED:
            self.parseChallengeDeclinedResponse(data)
        elif seq == Protocol.CHALLENGE_RECEIVED:
            self.parseChallengeReceivedResponse(data)
        elif seq == Protocol.CHALLENGE_RETRACTED:
            self.parseChallengeCancelledResponse(data)
        elif seq == Protocol.JOINING_A_CHANNEL:
            self.parseJoinChannelResponse(data)
        elif seq == Protocol.SPECTATE_GRANTED:
            self.parseSpectateResponse(data)
        else:
            # in band response to our previous request
            self.dispatchInbandData(seq, data)

    def dispatchInbandData(self, seq, data):
        if not seq in self.tcpCommandsWaitingForResponse:
            logdebug().error("Sequence {} data {} not matched".format(seq, data))
            return

        origRequest = self.tcpCommandsWaitingForResponse[seq]
        del self.tcpCommandsWaitingForResponse[seq]

        if origRequest == Protocol.AUTH:
            self.parseAuthResponse(data)
        elif origRequest == Protocol.MOTD:
            self.parseMotdResponse(data)
        elif origRequest == Protocol.LIST_CHANNELS:
            self.parseListChannelsResponse(data)
        elif origRequest == Protocol.LIST_USERS:
            self.parseListUsersResponse(data)
        elif origRequest == Protocol.SPECTATE:
            status, data = Protocol.extractInt(data)
            if status != 0:
                self.sigStatusMessage.emit("Fail to spectate " + str(status))
        elif origRequest in [Protocol.WELCOME, Protocol.JOIN_CHANNEL, Protocol.TOGGLE_AFK,
                             Protocol.SEND_CHALLENGE, Protocol.CHAT, Protocol.ACCEPT_CHALLENGE,
                             Protocol.DECLINE_CHALLENGE, Protocol.CANCEL_CHALLENGE]:
            if len(data) == 4:
                status, data = Protocol.extractInt(data)
                if status != 0:
                    codestr = Protocol.codeToString(origRequest)
                    logdebug().error("{} failed, data {}".format(codestr, repr(data)))
                    self.sigActionFailed.emit(codestr)
            else:
                logdebug().error("Unknown response for {}; seq {}; data {}".format(
                    Protocol.codeToString(origRequest), seq, repr(data)))
        else:
            logdebug().error("Not handling {} response; seq {}; data {}".format(
                Protocol.codeToString(origRequest), seq, repr(data)))

    @staticmethod
    def extractStateChangesResponse(data):
        if len(data) >= 4:
            code, data = Protocol.extractInt(data)
            p1, data = Protocol.extractTLV(data)
            if code == 0:
                p2 = ''
                return PlayerStates.QUIT, p1, p2, None, data
            elif code != 1:
                logdebug().error("Unknown player state change code {}".format(code))
            state, data = Protocol.extractInt(data)
            p2, data = Protocol.extractTLV(data)
            if not p2:
                p2 = "null"
            ip, data = Protocol.extractTLV(data)
            # \xff\xff\xff\x9f
            # \x00\x00\x00&
            unknown1, data = Protocol.extractInt(data)
            unknown2, data = Protocol.extractInt(data)
            city, data = Protocol.extractTLV(data)
            cc, data = Protocol.extractTLV(data)
            if cc:
                cc = cc.lower()
            country, data = Protocol.extractTLV(data)
            # \x00\x00\x17y
            marker, data = Protocol.extractInt(data)
            playerinfo = dict(
                player=p1,
                ip=ip,
                city=city,
                cc=cc,
                country=country,
            )
            return state, p1, p2, playerinfo, data

    def getPlayerChallengerText(self, name):
        extrainfo = []
        if name in self.players:
            p = self.players[name]
            if p.ping:
                extrainfo.append('{}ms'.format(p.ping))
            if p.country:
                extrainfo.append(p.country.decode('utf-8', 'ignore'))
        extrainfo = ', '.join(extrainfo)
        if extrainfo:
            extrainfo = '({}) '.format(extrainfo)
        line = self.getPlayerPrefix(name, True)
        line += " challenged you - " + extrainfo
        line += "<a href='accept:" + name + "'><font color=green>accept</font></a>"
        line += " / <a href='decline:" + name + "'><font color=green>decline</font></a>"
        return line

    def getPlayerColor(self, name):
        if name == self.username:
            return '#ff0000'
        elif name in self.players:
            if hasattr(self.players[name], 'id'):
                return ColorTheme.getPlayerColor(self.players[name].id)
        return '#808080'

    def getPlayerFlag(self, name):
        if name in self.players:
            p = self.players[name]
            if p.cc:
                return "<img src=':/flags/{}.png'/> ".format(p.cc)

    def getPlayerPrefix(self, name, useFlag):
        c = self.getPlayerColor(name)
        icon = ''
        if useFlag:
            icon = self.getPlayerFlag(name)
            if icon==None:
                icon=''
        if useFlag:
            return '{}<b><font color="{}">{}</font></b> '.format(icon, c, cgi.escape(name))
        else:
            return '<b><font color="{}">{}</font></b> '.format(c, cgi.escape('<{}>'.format(name)))


    def ggpoPathJoin(self, *args):
        if self.fba:
            return os.path.join(os.path.dirname(self.fba), *args)
        return os.path.join(*args)

    def handleTcpResponse(self):
        if self.tcpReadState == self.STATE_TCP_READ_LEN:
            if len(self.tcpData) >= 4:
                self.tcpResponseLen, self.tcpData = Protocol.extractInt(self.tcpData)
                self.tcpReadState = self.STATE_TCP_READ_DATA
                self.handleTcpResponse()
        elif self.tcpReadState == self.STATE_TCP_READ_DATA:
            if len(self.tcpData) >= self.tcpResponseLen:
                # tcpResponseLen should be >= 4
                if self.tcpResponseLen < 4:
                    logdebug().error('Cannot handle TLV payload of less than 4 bytes')
                    self.tcpData = self.tcpData[self.tcpResponseLen:]
                    self.tcpResponseLen = 0
                    self.tcpReadState = self.STATE_TCP_READ_LEN
                    self.handleTcpResponse()
                else:
                    data = self.tcpData[:self.tcpResponseLen]
                    self.tcpData = self.tcpData[self.tcpResponseLen:]
                    seq = Protocol.unpackInt(data[0:4])
                    self.tcpResponseLen = 0
                    self.tcpReadState = self.STATE_TCP_READ_LEN
                    self.dispatch(seq, data[4:])
                    self.handleTcpResponse()

    def handleUdpResponse(self, dgram, addr):
        if not dgram:
            return
        command = dgram[0:9]
        secret = dgram[10:]
        remoteip, remoteport = addr
        if command == "GGPO PING":
            self.sendudp("GGPO PONG {}".format(secret), addr)
            logdebug().info("send GGPO PONG {} to {}".format(secret, repr(addr)))
        if dgram[0:9] == "GGPO PONG":
            if secret in self.pinglist:
                ip = self.pinglist[secret][0]
                name = self.pinglist[secret][1]
                t1 = self.pinglist[secret][2]
                t2 = time.time()
                if ip == remoteip:
                    self.updatePlayerPing(name, int((t2 - t1) * 1000))
                del self.pinglist[secret]

    def parseAuthResponse(self, data):
        if len(data) < 4:
            logdebug().error("Unknown auth response {}".format(repr(data)))
            return
        result, data = Protocol.extractInt(data)
        if result == 0:
            self.selectTimeout = 15
            self.sigLoginSuccess.emit()
        # password incorrect, user incorrect
        #if result == 0x6 or result == 0x4:
        else:
            if self.tcpSock:
                self.tcpSock.close()
                self.tcpConnected = False
            if self.udpSock:
                self.udpSock.close()
                self.udpConnected = False
            self.sigLoginFailed.emit()
            self.sigStatusMessage.emit("Login failed {}".format(result))

    def parseChallengeCancelledResponse(self, data):
        name, data = Protocol.extractTLV(data)
        if name in list(self.challengers):
            self.challengers.remove(name)
        if name in self.ignored:
            return
        self.sigChallengeCancelled.emit(name)

    def parseChallengeDeclinedResponse(self, data):
        name, data = Protocol.extractTLV(data)
        if name == self.challenged:
            self.challenged = None
        if name in self.ignored:
            return
        self.sigChallengeDeclined.emit(name)

    def parseChallengeReceivedResponse(self, data):
        name, data = Protocol.extractTLV(data)
        rom, data = Protocol.extractTLV(data)
        if rom != self.rom or name in self.ignored:
            return
        self.challengers.add(name)
        self.sigChallengeReceived.emit(name)

    def parseChatResponse(self, data):
        name, data = Protocol.extractTLV(data)
        if name in self.ignored:
            return
        msg, data = Protocol.extractTLV(data)
        try:
            msg = msg.decode('utf-8')
        except ValueError:
            msg = msg
        if Settings.USER_LOG_CHAT:
            loguser().info(u"<{}> {}".format(name, msg))
        self.sigChatReceived.emit(name, msg)

    # noinspection PyUnusedLocal
    def parseJoinChannelResponse(self, data):
        self.sigChannelJoined.emit()
        self.sendMOTDRequest()
        self.sendListUsers()

    def parseListChannelsResponse(self, data):
        self.channels = {}
        if len(data) <= 8:
            logdebug().error('No channels found')
            self.sigChannelsLoaded.emit()
            return
        status1, data = Protocol.extractInt(data)
        status2, data = Protocol.extractInt(data)
        logdebug().info("Load channels header " + repr(status1) + repr(status2))
        while len(data) > 4:
            room, data = Protocol.extractTLV(data)
            romname, data = Protocol.extractTLV(data)
            title, data = Protocol.extractTLV(data)
            unknown, data = Protocol.extractInt(data)
            # 'sfa3': {'title': 'Street Fighter Alpha 3', 'rom': 'sfa3:sfa3u', 'room': 'sfa3'},
            # 'sfa2': {'title': 'Street Fighter Alpha 2', 'rom': 'sfa2', 'room': 'sfa2'},
            channel = {
                'rom': romname.split(':')[0],
                'room': room,
                'title': title,
            }
            self.channels[room] = channel
        logdebug().info(repr(self.channels))
        self.sigChannelsLoaded.emit()
        if len(data) > 0:
            logdebug().error('Channel REMAINING DATA len {} {}'.format(len(data), repr(data)))

    def parseListUsersResponse(self, data):
        self.resetPlayers()
        if not data:
            return
        status, data = Protocol.extractInt(data)
        status2, data = Protocol.extractInt(data)
        while len(data) > 8:
            p1, data = Protocol.extractTLV(data)
            # if len(data) <= 4: break
            state, data = Protocol.extractInt(data)
            p2, data = Protocol.extractTLV(data)
            ip, data = Protocol.extractTLV(data)
            unk1, data = Protocol.extractInt(data)
            unk2, data = Protocol.extractInt(data)
            city, data = Protocol.extractTLV(data)
            cc, data = Protocol.extractTLV(data)
            if cc:
                cc = cc.lower()
            country, data = Protocol.extractTLV(data)
            port, data = Protocol.extractInt(data)
            self.addUser(
                player=p1,
                ip=ip,
                port=port,
                city=city,
                cc=cc,
                country=country,
            )
            if state == PlayerStates.AVAILABLE:
                self.available[p1] = True
            elif state == PlayerStates.AFK:
                self.awayfromkb[p1] = True
            elif state == PlayerStates.PLAYING:
                if not p2:
                    p2 = 'null'
                self.playing[p1] = p2
        self.sigPlayersLoaded.emit()
        if len(data) > 0:
            logdebug().error('List users - REMAINING DATA len {} {}'.format(len(data), repr(data)))

    def parseMotdResponse(self, data):
        if not data:
            return
        status, data = Protocol.extractInt(data)
        channel, data = Protocol.extractTLV(data)
        topic, data = Protocol.extractTLV(data)
        msg, data = Protocol.extractTLV(data)
        self.sigMotdReceived.emit(channel, topic, msg)

    def parsePlayerAFKResponse(self, p1, playerinfo):
        self.addUser(**playerinfo)
        self.awayfromkb[p1] = True
        self.available.pop(p1, None)
        self.playing.pop(p1, None)
        self.sigPlayerStateChange.emit(p1, PlayerStates.AFK)

    def parsePlayerAvailableResponse(self, p1, playerinfo):
        self.addUser(**playerinfo)
        self.available[p1] = True
        self.awayfromkb.pop(p1, None)
        self.playing.pop(p1, None)
        self.sigPlayerStateChange.emit(p1, PlayerStates.AVAILABLE)

    def parsePlayerLeftResponse(self, p1):
        if p1:
            self.available.pop(p1, None)
            self.awayfromkb.pop(p1, None)
            self.playing.pop(p1, None)
            if p1 in self.challengers:
                self.challengers.remove(p1)
            if p1 == self.challenged:
                self.challenged = None
            self.sigPlayerStateChange.emit(p1, PlayerStates.QUIT)

    def parsePlayerStartGameResponse(self, p1, p2, playerinfo):
        self.addUser(**playerinfo)
        self.playing[p1] = p2
        self.available.pop(p1, None)
        self.awayfromkb.pop(p1, None)
        self.sigPlayerStateChange.emit(p1, PlayerStates.PLAYING)

    def parseSpectateResponse(self, data):
        p1, data = Protocol.extractTLV(data)
        p2, data = Protocol.extractTLV(data)
        # if the guy I challenged accepted, remove him as challenged
        if self.challenged and self.challenged in [p1, p2] and self.username in [p1, p2]:
            self.challenged = None
            # quark len(53) = 'quark:stream,ssf2t,challenge-07389-1393539605.46,7000'
        quark, data = Protocol.extractTLV(data)
        logdebug().info("Quark " + repr(quark))
        if quark.startswith('quark:served'):
            smooth = Settings.value(Settings.SMOOTHING)
            if smooth:
                match = re.search(r'[0-9]+', smooth)
                if match:
                    quark += ',{}'.format(match.group(0))
        self.runFBA(quark)

    def parseStateChangesResponse(self, data):
        count, data = Protocol.extractInt(data)
        while count > 0 and len(data) >= 4:
            state, p1, p2, playerinfo, data = self.__class__.extractStateChangesResponse(data)
            if state == PlayerStates.PLAYING:
                self.parsePlayerStartGameResponse(p1, p2, playerinfo)
		if self.username == p1:
			self.playingagainst = p2
		if self.username == p2:
			self.playingagainst = p1
                if Settings.USER_LOG_PLAYHISTORY and self.username in [p1, p2]:
                    loguser().info(u"[IN A GAME] {} vs {}".format(p1, p2))
            elif state == PlayerStates.AVAILABLE:
                self.parsePlayerAvailableResponse(p1, playerinfo)
                if self.playingagainst == p1:
                    self.playingagainst = ''
                    self.killEmulator()
            elif state == PlayerStates.AFK:
                self.parsePlayerAFKResponse(p1, playerinfo)
                if self.playingagainst == p1:
                    self.playingagainst = ''
                    self.killEmulator()
            elif state == PlayerStates.QUIT:
                self.parsePlayerLeftResponse(p1)
            else:
                logdebug().error(
                    "Unknown state change payload state: {} {}".format(state, repr(data)))
            if state == PlayerStates.PLAYING:
                msg = p1 + ' ' + PlayerStates.codeToString(state) + ' ' + p2
            else:
                msg = p1 + ' ' + PlayerStates.codeToString(state)
            logdebug().info(msg)
            count -= 1
        if len(data) > 0:
            logdebug().error("stateChangesResponse, remaining data {}".format(repr(data)))

    def killEmulator(self):
        if IS_WINDOWS:
            try:
                args = ['taskkill', '/F', '/PID', os.getpid()]
                Popen(args)
                args = ['tskill', 'ggpofba-ng', '/a']
                Popen(args)
            except:
                pass
        else:
            try:
                args = ['pkill', '-f', 'ggpofba-ng.exe.*quark:served']
                devnull = open(os.devnull, 'w')
                Popen(args, stdout=devnull, stderr=devnull)
                devnull.close()
            except:
                pass

    # platform independent way of playing an external wave file
    def playChallengeSound(self):
        if not self.fba:
            return
        wavfile = os.path.join(os.path.dirname(self.fba), "assets", "challenger-comes.wav")
        if not os.path.isfile(wavfile):
            return
        if IS_OSX:
            Popen(["afplay", wavfile])
        elif IS_WINDOWS and winsound:
            winsound.PlaySound(wavfile, winsound.SND_FILENAME)
        elif IS_LINUX:
            for cmd in ['/usr/bin/aplay', '/usr/bin/play', '/usr/bin/mplayer']:
                if os.path.isfile(cmd):
                    Popen([cmd, wavfile])
                    return

    def removeIgnore(self, player):
        if player in self.ignored:
            self.ignored.remove(player)
            self.saveIgnored()
            self.sigIgnoreRemoved.emit(player)

    def resetPlayers(self):
        self.available = {}
        self.playing = {}
        self.awayfromkb = {}

    def runFBA(self, quark):
        self.checkRom()
        self.fba = findFba()
        if not self.fba:
            self.sigStatusMessage.emit("Please configure Setting > Locate ggpofba-ng.exe")
            return
        args = []
        fba=self.fba
        if IS_WINDOWS:
            fba=fba.replace('ggpofba-ng.exe', 'ggpofba.exe')
        else:
            fba = fba.replace('ggpofba-ng.exe', 'ggpofba.sh')
        args = [fba, quark, '-w']

        logdebug().info(" ".join(args))
        try:
            # starting python from cmd.exe and redirect stderr and we got
            # python WindowsError(6, 'The handle is invalid')
            # apparently it's still not fixed
            if IS_WINDOWS:
                Popen(args)
            else:
                devnull = open(os.devnull, 'w')
                Popen(args, stdout=devnull, stderr=devnull)
                devnull.close()
        except OSError, ex:
            self.sigStatusMessage.emit("Error executing " + " ".join(args) + "\n" + repr(ex))

    def saveIgnored(self):
        Settings.setPythonValue(Settings.IGNORED, self.ignored)

    def selectLoop(self):
        while self.selectLoopRunning:
            inputs = []
            if self.udpConnected:
                inputs.append(self.udpSock)
            if self.tcpConnected:
                inputs.append(self.tcpSock)
                # windows doesn't allow select on 3 empty set
            if not inputs:
                time.sleep(1)
                continue
            inputready, outputready, exceptready = None, None, None
            # http://stackoverflow.com/questions/13414029/catch-interrupted-system-call-in-threading
            try:
                inputready, outputready, exceptready = select.select(inputs, [], [], self.selectTimeout)
            except select.error, ex:
                if ex[0] != errno.EINTR:
                    raise
            if not inputready:
                self.sendPingQueries()
            else:
                for stream in inputready:
                    if stream == self.tcpSock:
                        data = None
                        # noinspection PyBroadException
                        try:
                            data = stream.recv(8192)
                        except:
                            self.tcpConnected = False
                            self.selectLoopRunning = False
                            self.sigServerDisconnected.emit()
                            return
                        if data:
                            self.tcpData += data
                            self.handleTcpResponse()
                        else:
                            stream.close()
                            self.tcpConnected = False
                            self.selectLoopRunning = False
                            self.sigServerDisconnected.emit()
                    elif stream == self.udpSock:
                        dgram = None
                        # on windows xp
                        # Python exception: error: [Errno 10054]
                        # An existing connection was forcibly closed by the remote host
                        # noinspection PyBroadException
                        try:
                            dgram, addr = self.udpSock.recvfrom(64)
                        except:
                            pass
                        if dgram:
                            logdebug().info("UDP " + repr(dgram) + " from " + repr(addr))
                            self.handleUdpResponse(dgram, addr)

    def sendAcceptChallenge(self, name):
        if name in self.challengers:
            self.sendAndRemember(Protocol.ACCEPT_CHALLENGE, Protocol.packTLV(name) + Protocol.packTLV(self.rom))
            self.challengers.remove(name)

    def sendAndForget(self, command, data=''):
        logdebug().info('Sending {} seq {} {}'.format(Protocol.codeToString(command), self.sequence, repr(data)))
        self.sendtcp(struct.pack('!I', command) + data)

    def sendAndRemember(self, command, data=''):
        logdebug().info('Sending {} seq {} {}'.format(Protocol.codeToString(command), self.sequence, repr(data)))
        self.tcpCommandsWaitingForResponse[self.sequence] = command
        self.sendtcp(struct.pack('!I', command) + data)

    def sendAuth(self, username, password):
        self.username = username
        authdata = Protocol.packTLV(username) + Protocol.packTLV(password) + "\x00\x00\x17\x79"
        self.sendAndRemember(Protocol.AUTH, authdata)

    def sendCancelChallenge(self, name=None):
        if (name is None and self.challenged) or (name and name == self.challenged):
            self.sendAndRemember(Protocol.CANCEL_CHALLENGE, Protocol.packTLV(self.challenged))
            self.challenged = None

    def sendChallenge(self, name):
        self.sendCancelChallenge()
        self.sendAndRemember(Protocol.SEND_CHALLENGE, Protocol.packTLV(name) + Protocol.packTLV(self.rom))
        self.challenged = name

    def sendChat(self, line):
        if self.channel == 'unsupported' and self.unsupportedRom:
            line = '[' + self.unsupportedRom + '] ' + line
        line = line.encode('utf-8')
        self.sendAndRemember(Protocol.CHAT, Protocol.packTLV(line))

    def sendDeclineChallenge(self, name):
        self.sendAndRemember(Protocol.DECLINE_CHALLENGE, Protocol.packTLV(name))
        if name in self.challengers:
            self.challengers.remove(name)

    def sendJoinChannelRequest(self, channel=None):
        if channel:
            self.channel = channel
            Settings.setValue(Settings.SELECTED_CHANNEL, channel)
            if channel in self.channels:
                if channel != 'lobby':
                    self.rom = self.channels[channel]['rom']
            else:
                logdebug().error("Invalid channel {}".format(channel))
        self.sendAndRemember(Protocol.JOIN_CHANNEL, Protocol.packTLV(self.channel))

    def sendListChannels(self):
        self.sendAndRemember(Protocol.LIST_CHANNELS)

    def sendListUsers(self):
        self.sendAndRemember(Protocol.LIST_USERS)

    def sendMOTDRequest(self):
        self.sendAndRemember(Protocol.MOTD)

    def sendPingQueries(self):
        if self.udpConnected:
            for name in self.available.keys() + self.awayfromkb.keys() + self.playing.keys():
                p = self.players[name]
                self.sendPingQuery(p)

    def sendPingQuery(self, player):
        if not self.udpConnected:
            return
        if not player.ip:
            return
        if not player.port:
            player.port = 6009
        num1 = randint(500000, 30000000)
        num2 = randint(4000000, 900000000)
        secret = str(num1) + " " + str(num2)
        message = "GGPO PING " + secret
        logdebug().info("send GGPO PING {} to {}".format(secret, repr(player.ip)))
        self.sendudp(message, (player.ip, player.port, ))
        self.pinglist[secret] = (player.ip, player.player, time.time())

    def sendSpectateRequest(self, name):
        self.sendAndRemember(Protocol.SPECTATE, Protocol.packTLV(name))

    def sendToggleAFK(self, afk):
        if afk:
            val = 1
        else:
            val = 0
        self.sendAndRemember(Protocol.TOGGLE_AFK, Protocol.packInt(val))

    def sendWelcome(self):
        self.sendAndRemember(Protocol.WELCOME, '\x00\x00\x00\x00\x00\x00\x00\x1d\x00\x00\x00\x01')

    def sendtcp(self, msg):
        # length of whole packet = length of sequence + length of msg
        payloadLen = 4 + len(msg)
        # noinspection PyBroadException
        try:
            self.tcpSock.send(struct.pack('!II', payloadLen, self.sequence) + msg)
        except:
            self.tcpConnected = False
            self.selectLoopRunning = False
            self.sigServerDisconnected.emit()
        self.sequence += 1

    def sendudp(self, msg, address):
        # noinspection PyBroadException
        try:
            self.udpSock.sendto(msg, address)
        except:
            pass

    def setUnsupportedRom(self, rom):
        self.unsupportedRom = rom

    def statusBarMessage(self):
        u = len(self.playing) + len(self.available) + len(self.awayfromkb)
        if self.channel in self.channels:
            c = self.channels[self.channel]
            title = c['title']
        else:
            title = self.channel
        msg = '[{}] {} ({})'.format(self.username, title, u)
        if self.challengers:
            msg += " - INCOMING CHALLENGE!"
        return msg

    def updatePlayerPing(self, name, ping):
        if name in self.players:
            self.players[name].ping = ping
