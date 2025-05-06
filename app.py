from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_session import Session
import random
import string
import time
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)
socketio = SocketIO(app, manage_session=False)

games = {}  # In-memory store for games

# Briscola deck and logic
SUITS = ['Cups', 'Coins', 'Swords', 'Clubs']
RANKS = ['A', '3', 'K', 'Q', 'J', '7', '6', '5', '4', '2']
POINTS = {'A': 11, '3': 10, 'K': 4, 'Q': 3, 'J': 2, '7': 0, '6': 0, '5': 0, '4': 0, '2': 0}

# Helper functions
def generate_game_code(length=5):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def create_deck():
    return [{'suit': s, 'rank': r} for s in SUITS for r in RANKS]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/create', methods=['POST'])
def create():
    code = generate_game_code()
    deck = create_deck()
    random.shuffle(deck)
    briscola_card = deck.pop()  # Select and remove briscola from deck
    games[code] = {
        'players': [],
        'deck': deck,  # Deck now doesn't contain the briscola card
        'briscola': briscola_card,  # Store the briscola card
        'hands': {},
        'table': [],
        'turn': 0,
        'start_time': time.time(),
        'scores': {0: 0, 1: 0},
        'won_cards': {0: [], 1: []},  # Add won_cards to game state
        'active': True
    }
    session['game_code'] = code
    session['player_id'] = 0
    return redirect(url_for('game', code=code))

@app.route('/join', methods=['POST'])
def join():
    code = request.form.get('code').upper()
    if code in games and len(games[code]['players']) < 2:
        session['game_code'] = code
        session['player_id'] = 1
        return redirect(url_for('game', code=code))
    return redirect(url_for('index'))

@app.route('/game/<code>')
def game(code):
    if code not in games:
        return redirect(url_for('index'))
    return render_template('game.html', code=code)

# --- SocketIO Events and Game Logic ---

def deal_hands(deck):
    return [deck.pop(), deck.pop(), deck.pop()]

def get_next_player(turn):
    return (turn + 1) % 2

def is_player_turn(game, sid):
    return game['players'][game['turn']] == sid

def get_game_state(game, player_sid):
    player_idx = game['players'].index(player_sid)
    opponent_idx = (player_idx + 1) % 2
    # Calculate remaining cards, considering the briscola card if not yet drawn
    remaining_cards = len(game['deck'])
    if 'drawn_briscola' not in game and game['briscola']:
        remaining_cards += 1
    if len(game['players']) < 2:
        return {
            'hand': [],
            'table': [],
            'briscola': game['briscola'],
            'can_play': False,
            'status': 'Waiting for second player to join...',
            'timer': 0,
            'my_score': 0,
            'opponent_score': 0,
            'my_won_cards': [],
            'remaining_cards': remaining_cards
        }
    return {
        'hand': game['hands'][player_sid],
        'table': game['table'],
        'briscola': game['briscola'],
        'can_play': is_player_turn(game, player_sid) and game['active'],
        'status': f"Your turn!" if is_player_turn(game, player_sid) and game['active'] else "Waiting for opponent...",
        'timer': max(0, 30 - int(time.time() - game['start_time'])),
        'my_score': game['scores'][player_idx],
        'opponent_score': game['scores'][opponent_idx],
        'my_won_cards': game['won_cards'][player_idx],
        'remaining_cards': remaining_cards
    }

@socketio.on('join_game')
def on_join_game(data):
    code = data['code']
    sid = request.sid
    if code not in games or not games[code]['active']:
        emit('game_state', {'status': 'Game not found or inactive.', 'hand': [], 'table': [], 'briscola': None, 'can_play': False, 'timer': 0})
        return
    game = games[code]
    if sid not in game['players'] and len(game['players']) < 2:
        game['players'].append(sid)
    join_room(code)
    # Only start game when both players have joined
    if len(game['players']) == 2 and 'dealt' not in game:
        for psid in game['players']:
            game['hands'][psid] = deal_hands(game['deck'])
        game['dealt'] = True
        game['start_time'] = time.time()
    # Send state to all joined players
    for psid in game['players']:
        socketio.emit('game_state', get_game_state(game, psid), room=psid)

@socketio.on('play_card')
def on_play_card(data):
    code = data['code']
    card_data = data['card']  # Renamed to avoid confusion with card objects
    sid = request.sid
    if code not in games:
        return
    game = games[code]
    if not is_player_turn(game, sid) or not game['active']:
        return
    hand = game['hands'][sid]
    played_card = None
    for c in hand:
        if c['suit'] == card_data['suit'] and c['rank'] == card_data['rank']:
            played_card = c
            break
    if not played_card:
        return
    hand.remove(played_card)
    game['table'].append({'player': sid, 'card': played_card})
    # If only one card on table, it's next player's turn
    if len(game['table']) == 1:
        game['turn'] = get_next_player(game['players'].index(sid))
    # If both played, resolve trick
    elif len(game['table']) == 2:
        winner_idx = resolve_trick(game)
        game['turn'] = winner_idx
        game['table'] = []
        for psid in game['players']:
            if game['deck']:  # If cards are still in the main deck
                game['hands'][psid].append(game['deck'].pop())
            elif game['briscola'] and 'drawn_briscola' not in game:  # If deck is empty and briscola not yet drawn
                # The player who won the trick (and thus plays first now) draws the briscola card
                if game['players'][winner_idx] == psid:
                    game['hands'][psid].append(game['briscola'])
                    game['drawn_briscola'] = True  # Mark briscola as drawn
        if all(len(game['hands'][psid]) == 0 for psid in game['players']):
            game['active'] = False
            scores = game['scores']
            status = 'Draw!'
            if scores[0] > scores[1]:
                status = 'Player 1 wins!'
            elif scores[1] > scores[0]:
                status = 'Player 2 wins!'
            for psid in game['players']:
                socketio.emit('game_state', {**get_game_state(game, psid), 'status': status}, room=psid)
            return
    game['start_time'] = time.time()
    for psid in game['players']:
        socketio.emit('game_state', get_game_state(game, psid), room=psid)

def resolve_trick(game):
    briscola_suit = game['briscola']['suit']
    c0 = game['table'][0]['card']
    c1 = game['table'][1]['card']
    p0_sid = game['table'][0]['player']
    p1_sid = game['table'][1]['player']
    # Determine winner
    def card_value(card):
        return POINTS[card['rank']]
    def is_briscola(card):
        return card['suit'] == briscola_suit
    winner_on_table_idx = 0  # 0 for first card, 1 for second
    if is_briscola(c0) and not is_briscola(c1):
        winner_on_table_idx = 0
    elif is_briscola(c1) and not is_briscola(c0):
        winner_on_table_idx = 1
    elif c0['suit'] == c1['suit']:
        winner_on_table_idx = 0 if RANKS.index(c0['rank']) < RANKS.index(c1['rank']) else 1
    else:
        winner_on_table_idx = 0  # First player wins if suits differ and neither is briscola
    # Assign points and cards to the winner
    winner_sid = game['table'][winner_on_table_idx]['player']
    winner_player_list_idx = game['players'].index(winner_sid)
    game['scores'][winner_player_list_idx] += card_value(c0) + card_value(c1)
    game['won_cards'][winner_player_list_idx].append(c0)
    game['won_cards'][winner_player_list_idx].append(c1)
    return winner_player_list_idx

# Timer enforcement
@socketio.on('connect')
def on_connect():
    pass

@socketio.on('disconnect')
def on_disconnect():
    # Optionally handle player disconnects
    pass

def timer_check():
    now = time.time()
    for code, game in list(games.items()):
        if game['active'] and len(game['players']) == 2:
            if now - game['start_time'] > 30:
                game['active'] = False
                for psid in game['players']:
                    socketio.emit('game_state', {**get_game_state(game, psid), 'status': 'Time expired! Game over.'}, room=psid)

def background_timer():
    while True:
        timer_check()
        time.sleep(1)

threading.Thread(target=background_timer, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, debug=True)
