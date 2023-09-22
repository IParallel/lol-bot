"""
Handles league client and contains leveling loop that runs indefinitely
"""

import logging
import random
import game
import utils
import api
import account
from time import sleep
from constants import *
from launcher import Launcher

class ClientError(Exception):
    """Exception that signals an error has occurred in the League of Legends Client"""
    pass

class Client:
    """Client class that handles league client tasks needed to start a game"""

    def __init__(self):
        self.connection = api.Connection()
        self.log = logging.getLogger(__name__)
        self.launcher = Launcher()
        self.username = ""
        self.password = ""
        self.account_level = 0
        self.phase = ""
        self.prev_phase = ""
        self.client_errors = 0
        self.phase_errors = 0

    def account_loop(self):
        """Loop that handles the continuous leveling of accounts, takes about 3-4 days of leveling to complete"""
        while True:
            try:
                self.launcher.launch_league(account.get_username(), account.get_password())
                self.leveling_loop()
                utils.close_processes()
                account.set_account_as_leveled()
                self.client_errors = 0
            except ClientError:
                self.client_errors += 1
                if self.client_errors == MAX_ERRORS:
                    raise Exception("Max errors reached.Exiting.")
                utils.close_processes()

    def leveling_loop(self):
        """Main loop that runs the correct function based on the phase of the League Client, continuously starts games"""
        while not self.account_leveled():
            match self.get_phase():
                case 'None':
                    self.create_lobby(GAME_LOBBY_ID)
                case 'Lobby':
                    self.start_matchmaking(GAME_LOBBY_ID)
                case 'Matchmaking':
                    self.queue()
                case 'ReadyCheck':
                    self.accept_match()
                case 'ChampSelect':
                    self.game_lobby()
                case 'InProgress':
                    game.play_game()
                case 'Reconnect':
                    self.reconnect()
                case 'WaitingForStats':
                    self.wait_for_stats()
                case 'PreEndOfGame':
                    self.pre_end_of_game()
                case 'EndOfGame':
                    self.end_of_game()
                case _:
                    self.log.warning("Unknown phase: {}".format(self.phase))
                    raise ClientError

    def get_phase(self):
        """Requests the League Client phase"""
        for i in range(15):
            r = self.connection.request('get', '/lol-gameflow/v1/gameflow-phase')
            if r.status_code == 200:
                self.prev_phase = self.phase
                self.phase = r.json()
                self.log.debug("New Phase: {}, Previous Phase: {}".format(self.phase, self.prev_phase))
                if self.prev_phase == self.phase:
                    self.phase_errors += 1
                    if self.phase_errors == 15:
                        self.log.error("Transition error. Phase will not change.")
                        raise ClientError
                    else:
                        self.log.warning("Phase same as previous. Errno {}".format(self.phase_errors))
                else:
                    self.phase_errors = 0
                sleep(1.5)
                return self.phase
            sleep(1)
        self.log.warning("Could not get phase.")
        raise ClientError

    def create_lobby(self, lobby_id):
        """Creates a lobby for given lobby ID"""
        self.log.info("Creating lobby with lobby_id: {}".format(lobby_id))
        self.connection.request('post', '/lol-lobby/v2/lobby', data={'queueId': lobby_id})
        sleep(1.5)

    def start_matchmaking(self, lobby_id):
        """Starts matchmaking for a given lobby ID, will also wait out dodge timers"""
        self.log.info("Starting queue for lobby_id: {}".format(lobby_id))
        r = self.connection.request('get', '/lol-lobby/v2/lobby')
        if r.json()['gameConfig']['queueId'] != lobby_id:
            self.create_lobby(lobby_id)
            sleep(1)
        self.connection.request('post', '/lol-lobby/v2/lobby/matchmaking/search')
        sleep(1.5)

        # Check for dodge timer
        r = self.connection.request('get', '/lol-matchmaking/v1/search')
        if r.status_code == 200 and len(r.json()['errors']) != 0:
            dodge_timer = int(r.json()['errors'][0]['penaltyTimeRemaining'])
            self.log.info("Dodge Timer. Time Remaining: {}".format(utils.seconds_to_min_sec(dodge_timer)))
            sleep(dodge_timer)

    def queue(self):
        """Waits until the League Client Phase changes to something other than 'Matchmaking'"""
        self.log.info("In queue. Waiting for match.")
        while True:
            if self.get_phase() != 'Matchmaking':
                return
            sleep(1)

    def accept_match(self):
        """Accepts the Ready Check"""
        self.log.info("Accepting match")
        self.connection.request('post', '/lol-matchmaking/v1/ready-check/accept')

    def game_lobby(self):
        """Loop that handles tasks associated with the Champion Select Lobby"""
        self.log.debug("Lobby State: INITIAL. Time Left in Lobby: 90s. Action: Initialize.")
        r = self.connection.request('get', '/lol-champ-select/v1/session')
        if r.status_code != 200:
            return
        cs = r.json()

        r2 = self.connection.request('get', '/lol-lobby-team-builder/champ-select/v1/pickable-champion-ids')
        if r2.status_code != 200:
            return
        f2p = r2.json()

        champ_index = 0
        f2p_index = 0
        requested = False
        while r.status_code == 200:
            lobby_state = cs['timer']['phase']
            lobby_time_left = int(float(cs['timer']['adjustedTimeLeftInPhase']) / 1000)

            # Find player action
            for action in cs['actions'][0]:  # There are 5 actions in the first action index, one for each player
                if action['actorCellId'] != cs['localPlayerCellId']:  # determine which action corresponds to bot
                    continue

                # Check if champ is already locked in
                if not action['completed']:
                    # Select Champ or Lock in champ that has already been selected
                    if action['championId'] == 0:  # no champ selected, attempt to select a champ
                        self.log.info("Lobby State: {}. Time Left in Lobby: {}s. Action: Hovering champ.".format(lobby_state, lobby_time_left))

                        if champ_index < len(CHAMPS):
                            champion_id = CHAMPS[champ_index]
                            champ_index += 1
                        else:
                            champion_id = f2p[f2p_index]
                            f2p_index += 1

                        url = '/lol-champ-select/v1/session/actions/{}'.format(action['id'])
                        data = {'championId': champion_id}
                        self.connection.request('patch', url, data=data)
                    else:  # champ selected, lock in
                        self.log.info("Lobby State: {}. Time Left in Lobby: {}s. Action: Locking in champ.".format(lobby_state, lobby_time_left))
                        url = '/lol-champ-select/v1/session/actions/{}'.format(action['id'])
                        data = {'championId': action['championId']}
                        self.connection.request('post', url + '/complete', data=data)

                        # Ask for mid
                        if not requested:
                            sleep(1)
                            try:  # if the ASK_4_MID_DIALOG is empty this will error
                                self.chat(random.choice(ASK_4_MID_DIALOG), 'handle_game_lobby')
                            except:
                                pass
                            requested = True
                else:
                    self.log.debug("Lobby State: {}. Time Left in Lobby: {}s. Action: Waiting".format(lobby_state, lobby_time_left))
                r = self.connection.request('get', '/lol-champ-select/v1/session')
                if r.status_code != 200:
                    self.log.info('Lobby State: CLOSED. Time Left in Lobby: 0s. Action: Exit.')
                    return
                cs = r.json()
                sleep(3)

    def reconnect(self):
        """Attempts to reconnect to an ongoing League of Legends match"""
        for i in range(3):
            r = self.connection.request('post', '/lol-gameflow/v1/reconnect')
            if r.status_code == 204:
                return
            sleep(2)
        self.log.warning('Could not reconnect to game')

    def wait_for_stats(self):
        """
        Waits for the League Client Phase to change to something other than 'WaitingForStats'
        Often times disconnects will happen after a game finishes and the league client will
        only return the phase 'WaitingForStats' which causes a ClientError.
        """
        self.log.info("Waiting for stats.")
        for i in range(60):
            sleep(2)
            if self.get_phase() != 'WaitingForStats':
                return
        self.log.warning("Waiting for stats timeout.")
        raise ClientError

    def pre_end_of_game(self):
        """
        Handles league of legends client reopening, honoring teamates, and clearing level-up/mission rewards
        This function should hopefully be updated to not include any clicks on the client, but I currently do not know
        of any endpoints that can clear the 'send email' popup or mission/level rewards
        """

        self.log.info("Honoring teammates and accepting rewards.")
        sleep(3)

        # occasionally the lcu-api will be ready before the actual client window appears
        # in this instance, the utils.click will throw an exception. just catch and wait
        try:
            utils.click(POPUP_SEND_EMAIL_X_RATIO, LEAGUE_CLIENT_WINNAME, 1)
            sleep(1)
            self.honor_player()
            sleep(2)
            utils.click(POPUP_SEND_EMAIL_X_RATIO, LEAGUE_CLIENT_WINNAME, 1)
            sleep(1)
            for i in range(3):
                utils.click(POST_GAME_SELECT_CHAMP_RATIO, LEAGUE_CLIENT_WINNAME, 1)
                utils.click(POST_GAME_OK_RATIO, LEAGUE_CLIENT_WINNAME, 1)
            utils.click(POPUP_SEND_EMAIL_X_RATIO, LEAGUE_CLIENT_WINNAME, 1)
        except:
            sleep(3)

    def end_of_game(self) -> None:
        """
        Transitions League Client to 'EndOfGame' OR 'Lobby' phase. Occasionally, posting
        to the play-again endpoint just does not work and the phase must be manually changed to 'Lobby'
        or raise a ClientError
        """

        posted = False
        for i in range(15):
            if self.get_phase() != 'EndOfGame':
                return
            if not posted:
                self.connection.request('post', '/lol-lobby/v2/play-again')
            else:
                self.create_lobby(GAME_LOBBY_ID)
            posted = not posted
            sleep(1)
        self.log.warning("Could not exit play-again screen.")
        raise ClientError

    def account_leveled(self) -> bool:
        """Checks if account has reached the constants.MAX_LEVEL (default 30)"""
        r = self.connection.request('get', '/lol-chat/v1/me')
        if r.status_code == 200:
            self.account_level = r.json()['lol']['level']
            if self.account_level < ACCOUNT_MAX_LEVEL:
                self.log.info("ACCOUNT LEVEL: {}.".format(self.account_level))
                return False
            else:
                self.log.info("SUCCESS: Account Leveled")
                return True

    def check_patch(self) -> None:
        """Checks if the League Client is patching and waits till it is finished"""
        self.log.info("Checking for Client Updates")
        r = self.connection.request('get', '/patcher/v1/products/league_of_legends/state')
        if r.status_code != 200:
            return
        logged = False
        while not r.json()['isUpToDate']:
            if not logged:
                self.log.info("Client is patching...")
                logged = True
            sleep(3)
            r = self.connection.request('get', '/patcher/v1/products/league_of_legends/state')
            self.log.debug('Status Code: {}, Percent Patched: {}%'.format(r.status_code, r.json()['percentPatched']))
            self.log.debug(r.json())
        self.log.info("Client is up to date!")

    def honor_player(self):
        """Honors a player in the post game lobby"""
        for i in range(3):
            r = self.connection.request('get', '/lol-honor-v2/v1/ballot')
            if r.status_code == 200:
                players = r.json()['eligiblePlayers']
                index = random.randint(0, len(players)-1)
                self.connection.request('post', '/lol-honor-v2/v1/honor-player', data={"summonerId": players[index]['summonerId']})
                self.log.info("Honor Success: Player {}. Champ: {}. Summoner: {}. ID: {}".format(index+1, players[index]['championName'], players[index]['summonerName'], players[index]['summonerId']))
                return
            sleep(2)
        self.log.info('Honor Failure. Player -1, Champ: NULL. Summoner: NULL. ID: -1')
        self.connection.request('post', '/lol-honor-v2/v1/honor-player', data={"summonerId": 0})  # will clear honor screen

    def chat(self, msg, calling_func_name='') -> None:
        """Sends a message to the chat window"""
        chat_id = ''
        r = self.connection.request('get', '/lol-chat/v1/conversations')
        if r.status_code != 200:
            if calling_func_name != '':
                self.log.warning("{} chat attempt failed. Could not reach endpoint".format(calling_func_name))
            else:
                self.log.warning("Could not reach endpoint")
            return

        for convo in r.json():
            if convo['gameName'] != '' and convo['gameTag'] != '':
                continue
            chat_id = convo['id']

        if chat_id == '':
            if calling_func_name != '':
                self.log.warning('{} chat attempt failed. Could not send message. Chat ID is Null'.format(calling_func_name))
            else:
                self.log.warning('Could not send message. Chat ID is Null')
            return

        data = {"body": msg}
        r = self.connection.request('post', '/lol-chat/v1/conversations/{}/messages'.format(chat_id), data=data)
        if r.status_code != 200:
            if calling_func_name != '':
                self.log.warning('{}, could not send message. HTTP STATUS: {} - {}'.format(calling_func_name, r.status_code, r.json()))
            else:
                self.log.warning('Could not send message. HTTP STATUS: {} - {}'.format(r.status_code, r.json()))
        else:
            if calling_func_name != '':
                self.log.debug("{}, message success. Msg: {}".format(calling_func_name, msg))
            else:
                self.log.debug("Message Success. Msg: {}".format(msg))
