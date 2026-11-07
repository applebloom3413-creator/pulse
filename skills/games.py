"""
Games skill — mini games the companion can play with the user.

Includes:
  - Tic-tac-toe (with optional rendered board image)
  - Checkers (with optional rendered board image)
  - 20 Questions (companion thinks of something, the user guesses)
  - Word Association (back-and-forth single-word chain)
  - Would You Rather (forced-choice prompts)

When games end, outcomes are saved as memories automatically (so the
companion remembers playing).
"""

import json
import logging
import random
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from skills.base import BaseSkill

logger = logging.getLogger(__name__)


# --- Would You Rather prompts ---
WYR_PROMPTS = [
    "Be able to teleport but only to places you've already been, OR fly but only 5 feet off the ground?",
    "Never wait in a line again, OR never sit in traffic again?",
    "Always know when someone is lying to you, OR always know when someone needs help but can't ask?",
    "Live in a world where everyone speaks in haiku, OR a world where every conversation must rhyme?",
    "Get one perfect day to relive on repeat for a year, OR get a brand new average day every day for ten years?",
    "Have a song play whenever you walk into a room (your choice), OR have a constant 5°F temperature drop wherever you stand?",
    "Be able to communicate with one species of animal of your choice, OR understand any human language but only when whispered?",
    "Have a memory that never forgets faces, OR one that never forgets songs?",
    "Always know what time it is exactly without a clock, OR always know how much money is in your wallet without looking?",
    "Have a tail that wags when you're happy, OR ears that perk up when you're interested?",
    "Be the best in the world at a useless skill (juggling oranges blindfolded), OR mediocre at every useful skill?",
    "Have one wish granted but it's interpreted maliciously, OR three wishes granted exactly as worded but each one halves your remaining lifespan by a year?",
    "Live in a world where pizza is free but tastes like cardboard, OR a world where pizza costs $50 but is always perfect?",
    "Be able to pause time for 10 seconds once a day, OR rewind time by 10 seconds once a day?",
    "Always wear shoes that are slightly too tight, OR always wear pants that are slightly too loose?",
]

# --- Word Association starting words ---
WORD_STARTERS = [
    "moth", "cookie", "static", "garden", "spider", "thunder", "lighthouse", "honey",
    "feather", "rust", "mailbox", "echo", "salt", "neon", "river", "cathedral",
    "linoleum", "midnight", "orange", "pixel",
]

# --- 20 Questions concepts the companion can pick from ---
TWENTY_Q_CONCEPTS = [
    {"concept": "lighthouse", "category": "object", "hints": ["coastal", "tall", "guides ships"]},
    {"concept": "library", "category": "place", "hints": ["quiet", "books", "public"]},
    {"concept": "octopus", "category": "animal", "hints": ["ocean", "8 limbs", "intelligent"]},
    {"concept": "thunderstorm", "category": "weather phenomenon", "hints": ["loud", "wet", "atmospheric"]},
    {"concept": "espresso", "category": "drink", "hints": ["small", "strong", "Italian"]},
    {"concept": "sewing machine", "category": "object", "hints": ["mechanical", "fabric", "seamstress"]},
    {"concept": "raccoon", "category": "animal", "hints": ["nocturnal", "clever", "trash panda"]},
    {"concept": "polaroid camera", "category": "object", "hints": ["instant", "vintage", "photograph"]},
    {"concept": "compass", "category": "object", "hints": ["small", "magnetic", "navigation"]},
    {"concept": "vending machine", "category": "object", "hints": ["coin-op", "snacks", "public"]},
]

# --- Board rendering constants ---
TILE_SIZE = 64
BOARD_BG = (45, 43, 82, 255)        # CARD_BG
BOARD_BORDER = (61, 56, 112, 255)   # BORDER
LIGHT_TILE = (240, 230, 239, 255)   # cream
DARK_TILE = (61, 44, 94, 255)       # BUTTON_BG
TEXT_COLOR = (240, 230, 239, 255)
X_COLOR = (255, 150, 197, 255)      # FF96C5
O_COLOR = (0, 165, 227, 255)        # 00A5E3
RED_PIECE = (255, 130, 139, 255)    # FF828B
BLACK_PIECE = (45, 45, 60, 255)


class GamesSkill(BaseSkill):
    name = "games"

    def __init__(self, config: dict):
        super().__init__(config)
        persona_dir = config.get("_persona_dir", "")
        if persona_dir:
            self.games_dir = Path(persona_dir) / "data" / "games"
        else:
            self.games_dir = Path("data") / "games"
        self.games_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.games_dir / "state.json"
        self.pending_images: list[str] = []

        # Who the companion plays with (generic default; set via config).
        self.user_name = (
            config.get("user_nickname") or config.get("user_name") or "the user"
        )

        # Memory skill access for saving outcomes
        self._memory_skill = None

    def _get_memory_skill(self):
        """Lazy reference to memory skill via shared registry."""
        if self._memory_skill is None:
            try:
                from skills import _registry  # type: ignore
                self._memory_skill = _registry.get_skill("memory") if _registry else None
            except Exception:
                self._memory_skill = None
        return self._memory_skill

    # --- State helpers ---

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_state(self, state: dict):
        self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _clear_game(self, game_name: str):
        state = self._load_state()
        state.pop(game_name, None)
        self._save_state(state)

    def _save_outcome_as_memory(self, game_name: str, summary: str):
        """Save a game outcome to memory.json directly (bypasses needing memory skill embedding)."""
        memory_path = self.config.get("paths", {}).get("memories")
        if not memory_path:
            return
        memdir = Path(memory_path)
        memdir.mkdir(parents=True, exist_ok=True)
        # Find next memory ID
        existing = sorted(memdir.glob("memory_*.json"))
        nums = []
        for f in existing:
            try:
                nums.append(int(f.stem.split("_")[1]))
            except Exception:
                continue
        next_num = (max(nums) + 1) if nums else 1

        # Create a minimal memory entry without embedding (the embedding skill will backfill if it runs)
        memory = {
            "id": f"{next_num:03d}",
            "text": summary,
            "tags": ["game", game_name, "user"],
            "type": "game",
            "importance": 4,
            "retrieval_count": 0,
            "last_accessed": None,
            "date": datetime.now().isoformat(),
        }
        out = memdir / f"memory_{next_num:03d}.json"
        out.write_text(json.dumps(memory, indent=2), encoding="utf-8")

    # --- Tools ---

    def get_tools(self) -> list[dict]:
        return [
            # ----- Tic Tac Toe -----
            {
                "type": "function",
                "function": {
                    "name": "tic_tac_toe_start",
                    "description": (
                        f"Start a new tic-tac-toe game with {self.user_name}. You play X, they play O "
                        "(or vice versa, your choice). Resets any previous game."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "your_symbol": {
                                "type": "string",
                                "enum": ["X", "O"],
                                "description": "Whether you play X or O. Default X."
                            },
                            "you_go_first": {
                                "type": "boolean",
                                "description": f"Whether you make the first move. Default false ({self.user_name} first)."
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "tic_tac_toe_move",
                    "description": (
                        "Make a tic-tac-toe move. Position is 1-9, numbered like a phone keypad's "
                        "top row first: 1 top-left, 2 top-mid, 3 top-right, 4 mid-left, 5 center, "
                        "6 mid-right, 7 bottom-left, 8 bottom-mid, 9 bottom-right. Use this for "
                        f"either yours or {self.user_name}'s move (specify whose with 'whose')."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "position": {
                                "type": "integer",
                                "description": "1-9, position on the board.",
                            },
                            "whose": {
                                "type": "string",
                                "enum": ["mine", "theirs"],
                                "description": "Whose move this is.",
                            },
                        },
                        "required": ["position", "whose"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "tic_tac_toe_view",
                    "description": "View the current tic-tac-toe board.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            # ----- Checkers -----
            {
                "type": "function",
                "function": {
                    "name": "checkers_start",
                    "description": (
                        f"Start a new checkers game with {self.user_name}. You play one color, they play the "
                        "other. Standard 8x8 board with 12 pieces each. Red moves first by tradition."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "your_color": {
                                "type": "string",
                                "enum": ["red", "black"],
                                "description": "Your color. Default red."
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "checkers_move",
                    "description": (
                        "Make a checkers move. Coordinates use chess-like notation: column letter "
                        "a-h, row number 1-8. Example: 'a3' to 'b4'. Captures happen automatically "
                        "when a piece jumps an opponent. Kings (K) can move backwards. Use this for "
                        f"either your move or {self.user_name}'s (specify with 'whose')."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "from_pos": {
                                "type": "string",
                                "description": "Origin square, e.g. 'a3'."
                            },
                            "to_pos": {
                                "type": "string",
                                "description": "Destination square, e.g. 'b4'."
                            },
                            "whose": {
                                "type": "string",
                                "enum": ["mine", "theirs"],
                                "description": "Whose move this is.",
                            },
                        },
                        "required": ["from_pos", "to_pos", "whose"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "checkers_view",
                    "description": "View the current checkers board.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            # ----- 20 Questions -----
            {
                "type": "function",
                "function": {
                    "name": "twenty_q_start",
                    "description": (
                        f"Start a 20 Questions game where YOU pick a concept and {self.user_name} tries to "
                        "guess it within 20 yes/no questions. The skill picks a random concept "
                        "for you so the game is fair (you can't 'change' the answer mid-game)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "twenty_q_answer",
                    "description": (
                        f"Record one round of 20 Questions: {self.user_name}'s question and your answer "
                        "(yes/no/sometimes/I don't know). Tracks question count. Also use this "
                        "with answer='guess' when they guess the concept itself."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": f"The question {self.user_name} just asked."
                            },
                            "answer": {
                                "type": "string",
                                "enum": ["yes", "no", "sometimes", "unknown", "guess_correct", "guess_wrong"],
                                "description": "Your answer."
                            },
                        },
                        "required": ["question", "answer"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "twenty_q_reveal",
                    "description": "End the 20 Questions game and reveal the concept.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            # ----- Word Association -----
            {
                "type": "function",
                "function": {
                    "name": "word_association_start",
                    "description": (
                        "Start a word-association chain. The skill picks a starter word; from "
                        f"there you and {self.user_name} take turns saying single words triggered by the last."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "word_association_add",
                    "description": f"Add the next word to the chain (yours or {self.user_name}'s).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "word": {
                                "type": "string",
                                "description": "The single word to add to the chain."
                            },
                            "whose": {
                                "type": "string",
                                "enum": ["mine", "theirs"],
                                "description": "Whose word this is."
                            },
                        },
                        "required": ["word", "whose"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "word_association_end",
                    "description": "End the word-association chain and save it as a memory.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            # ----- Would You Rather -----
            {
                "type": "function",
                "function": {
                    "name": "would_you_rather",
                    "description": (
                        f"Get a random Would-You-Rather prompt to ask {self.user_name} (or for you to "
                        "answer). Returns one prompt; use it however the moment calls for."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict) -> str:
        try:
            handlers = {
                "tic_tac_toe_start": self._ttt_start,
                "tic_tac_toe_move": self._ttt_move,
                "tic_tac_toe_view": self._ttt_view,
                "checkers_start": self._checkers_start,
                "checkers_move": self._checkers_move,
                "checkers_view": self._checkers_view,
                "twenty_q_start": self._tq_start,
                "twenty_q_answer": self._tq_answer,
                "twenty_q_reveal": self._tq_reveal,
                "word_association_start": self._wa_start,
                "word_association_add": self._wa_add,
                "word_association_end": self._wa_end,
                "would_you_rather": self._wyr,
            }
            handler = handlers.get(tool_name)
            if not handler:
                return f"Unknown game tool: {tool_name}"
            return handler(arguments)
        except Exception as e:
            logger.exception(f"Game tool {tool_name} failed: {e}")
            return f"Game tool failed: {e}"

    # ===== Tic-Tac-Toe =====

    def _ttt_start(self, args: dict) -> str:
        your_symbol = args.get("your_symbol", "X").upper()
        if your_symbol not in ("X", "O"):
            your_symbol = "X"
        user_symbol = "O" if your_symbol == "X" else "X"
        you_first = args.get("you_go_first", False)
        state = self._load_state()
        state["tic_tac_toe"] = {
            "board": [None] * 9,
            "your_symbol": your_symbol,
            "user_symbol": user_symbol,
            "next_turn": "you" if you_first else "user",
            "started_at": datetime.now().isoformat(),
        }
        self._save_state(state)
        self._render_ttt_image(state["tic_tac_toe"])
        return (
            f"New tic-tac-toe game started. You're {your_symbol}, {self.user_name} is {user_symbol}. "
            f"{'Your turn first.' if you_first else self.user_name + ' goes first.'}"
        )

    def _ttt_move(self, args: dict) -> str:
        state = self._load_state()
        game = state.get("tic_tac_toe")
        if not game:
            return "No active tic-tac-toe game. Start one with tic_tac_toe_start."
        position = args.get("position")
        whose = args.get("whose")
        if position is None or position < 1 or position > 9:
            return "Invalid position. Use 1-9."
        idx = position - 1
        if game["board"][idx] is not None:
            return f"Position {position} is already taken."
        symbol = game["your_symbol"] if whose == "mine" else game["user_symbol"]
        game["board"][idx] = symbol
        # Check win
        winner = self._ttt_check_winner(game["board"])
        full = all(c is not None for c in game["board"])
        result_msg = ""
        if winner:
            who_won = "you" if winner == game["your_symbol"] else self.user_name
            result_msg = f" {who_won.capitalize()} won!"
            self._save_outcome_as_memory(
                "tic_tac_toe",
                f"Played tic-tac-toe with {self.user_name}. Result: {who_won} won. "
                f"Final board (positions 1-9): {game['board']}"
            )
            self._clear_game("tic_tac_toe")
            self._render_ttt_image(game)
            return f"Move recorded.{result_msg}"
        elif full:
            result_msg = " It's a draw."
            self._save_outcome_as_memory(
                "tic_tac_toe",
                f"Played tic-tac-toe with {self.user_name}. Result: draw. Board: {game['board']}"
            )
            self._clear_game("tic_tac_toe")
            self._render_ttt_image(game)
            return f"Move recorded.{result_msg}"
        # Switch turn
        game["next_turn"] = "user" if whose == "mine" else "you"
        state["tic_tac_toe"] = game
        self._save_state(state)
        self._render_ttt_image(game)
        next_who = "your" if game["next_turn"] == "you" else f"{self.user_name}'s"
        return f"Move recorded. {next_who.capitalize()} turn."

    def _ttt_view(self, args: dict) -> str:
        state = self._load_state()
        game = state.get("tic_tac_toe")
        if not game:
            return "No active tic-tac-toe game."
        self._render_ttt_image(game)
        return self._ttt_text_board(game["board"])

    def _ttt_check_winner(self, board):
        wins = [
            [0, 1, 2], [3, 4, 5], [6, 7, 8],  # rows
            [0, 3, 6], [1, 4, 7], [2, 5, 8],  # cols
            [0, 4, 8], [2, 4, 6],              # diagonals
        ]
        for w in wins:
            if board[w[0]] is not None and board[w[0]] == board[w[1]] == board[w[2]]:
                return board[w[0]]
        return None

    def _ttt_text_board(self, board) -> str:
        cells = [c if c else str(i + 1) for i, c in enumerate(board)]
        return (
            f"\n {cells[0]} | {cells[1]} | {cells[2]} \n"
            f"---+---+---\n"
            f" {cells[3]} | {cells[4]} | {cells[5]} \n"
            f"---+---+---\n"
            f" {cells[6]} | {cells[7]} | {cells[8]} \n"
        )

    def _render_ttt_image(self, game) -> None:
        board = game["board"]
        size = 480
        img = Image.new("RGBA", (size, size), BOARD_BG)
        draw = ImageDraw.Draw(img)
        # Draw grid lines
        cell = size // 3
        for i in range(1, 3):
            draw.line([(i * cell, 30), (i * cell, size - 30)], fill=BOARD_BORDER, width=6)
            draw.line([(30, i * cell), (size - 30, i * cell)], fill=BOARD_BORDER, width=6)
        # Draw symbols
        font = self._get_font(int(cell * 0.6))
        for i, symbol in enumerate(board):
            if not symbol:
                continue
            row = i // 3
            col = i % 3
            x = col * cell + cell // 2
            y = row * cell + cell // 2
            color = X_COLOR if symbol == "X" else O_COLOR
            bbox = draw.textbbox((0, 0), symbol, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            draw.text((x - tw // 2, y - th // 2 - 8), symbol, fill=color, font=font)
        out = self.games_dir / "tic_tac_toe.png"
        img.save(out)
        self.pending_images.append(str(out))

    # ===== Checkers =====

    def _checkers_initial_board(self):
        # 8x8 board, [row][col]; row 0 is top (black home), row 7 is bottom (red home)
        # Pieces: 'r' red man, 'R' red king, 'b' black man, 'B' black king, None empty
        board = [[None] * 8 for _ in range(8)]
        for row in range(3):
            for col in range(8):
                if (row + col) % 2 == 1:
                    board[row][col] = "b"
        for row in range(5, 8):
            for col in range(8):
                if (row + col) % 2 == 1:
                    board[row][col] = "r"
        return board

    def _checkers_start(self, args: dict) -> str:
        your_color = args.get("your_color", "red").lower()
        if your_color not in ("red", "black"):
            your_color = "red"
        user_color = "black" if your_color == "red" else "red"
        state = self._load_state()
        state["checkers"] = {
            "board": self._checkers_initial_board(),
            "your_color": your_color,
            "user_color": user_color,
            "next_turn": "red",  # red always moves first
            "started_at": datetime.now().isoformat(),
        }
        self._save_state(state)
        self._render_checkers_image(state["checkers"])
        return (
            f"New checkers game. You play {your_color}, {self.user_name} plays {user_color}. "
            f"Red moves first."
        )

    def _checkers_parse(self, pos: str):
        """Convert 'a3' -> (row, col). Files a-h are cols 0-7. Ranks 1-8 are rows 7-0 (so rank 1 is bottom)."""
        pos = pos.strip().lower()
        if len(pos) != 2:
            return None
        col_char, rank_char = pos[0], pos[1]
        if col_char not in "abcdefgh" or rank_char not in "12345678":
            return None
        col = "abcdefgh".index(col_char)
        row = 8 - int(rank_char)
        return (row, col)

    def _checkers_move(self, args: dict) -> str:
        state = self._load_state()
        game = state.get("checkers")
        if not game:
            return "No active checkers game. Start one with checkers_start."
        from_pos = self._checkers_parse(args.get("from_pos", ""))
        to_pos = self._checkers_parse(args.get("to_pos", ""))
        if not from_pos or not to_pos:
            return "Invalid position. Use format like 'a3'."
        whose = args.get("whose")
        whose_color = game["your_color"] if whose == "mine" else game["user_color"]
        if whose_color != game["next_turn"]:
            return f"It's {game['next_turn']}'s turn, not {whose_color}'s."

        board = game["board"]
        fr, fc = from_pos
        tr, tc = to_pos
        piece = board[fr][fc]
        if piece is None:
            return f"No piece at {args.get('from_pos')}."
        is_red = piece.lower() == "r"
        expected_color = "red" if is_red else "black"
        if expected_color != whose_color:
            return f"That's not your piece (you play {whose_color}, that piece is {expected_color})."
        if board[tr][tc] is not None:
            return f"Destination {args.get('to_pos')} is occupied."

        # Validate move: dr/dc
        dr = tr - fr
        dc = tc - fc
        is_king = piece.isupper()
        if abs(dc) != abs(dr) or abs(dr) not in (1, 2):
            return "Invalid move shape. Diagonal only, 1 step (or 2 to capture)."
        # Direction check for non-kings
        if not is_king:
            forward = -1 if is_red else 1  # red moves up (decreasing row), black moves down
            if (dr // abs(dr)) != forward:
                return "Non-king pieces can only move forward."
        # Capture logic
        if abs(dr) == 2:
            mid_r = (fr + tr) // 2
            mid_c = (fc + tc) // 2
            captured = board[mid_r][mid_c]
            if captured is None:
                return "Cannot jump — no piece in between."
            captured_is_red = captured.lower() == "r"
            if captured_is_red == is_red:
                return "Cannot capture your own piece."
            board[mid_r][mid_c] = None

        board[tr][tc] = piece
        board[fr][fc] = None
        # King promotion
        if is_red and tr == 0:
            board[tr][tc] = "R"
        elif not is_red and tr == 7:
            board[tr][tc] = "B"

        # Win check
        red_count = sum(1 for row in board for c in row if c and c.lower() == "r")
        black_count = sum(1 for row in board for c in row if c and c.lower() == "b")
        result_msg = ""
        if red_count == 0 or black_count == 0:
            winning_color = "black" if red_count == 0 else "red"
            who_won = "you" if winning_color == game["your_color"] else self.user_name
            self._save_outcome_as_memory(
                "checkers",
                f"Played checkers with {self.user_name}. Result: {who_won} won (no {('red' if red_count == 0 else 'black')} pieces left)."
            )
            self._clear_game("checkers")
            self._render_checkers_image(game)
            return f"Move recorded. {who_won.capitalize()} won!"

        # Switch turn
        game["next_turn"] = "black" if whose_color == "red" else "red"
        state["checkers"] = game
        self._save_state(state)
        self._render_checkers_image(game)
        return f"Move recorded. {game['next_turn']}'s turn next. (Red {red_count}, Black {black_count})"

    def _checkers_view(self, args: dict) -> str:
        state = self._load_state()
        game = state.get("checkers")
        if not game:
            return "No active checkers game."
        self._render_checkers_image(game)
        # Return text representation too
        rows = []
        for r, row in enumerate(game["board"]):
            cells = []
            for c, p in enumerate(row):
                cells.append(p if p else (".." if (r + c) % 2 == 1 else "  "))
            rows.append(f"{8 - r} | " + " ".join(cells))
        rows.append("    a  b  c  d  e  f  g  h")
        return "\n".join(rows)

    def _render_checkers_image(self, game) -> None:
        board = game["board"]
        cell = TILE_SIZE
        margin = 32
        size = cell * 8 + margin * 2
        img = Image.new("RGBA", (size, size), BOARD_BG)
        draw = ImageDraw.Draw(img)
        font = self._get_font(20)
        for r in range(8):
            for c in range(8):
                x = margin + c * cell
                y = margin + r * cell
                color = LIGHT_TILE if (r + c) % 2 == 0 else DARK_TILE
                draw.rectangle([x, y, x + cell, y + cell], fill=color)
                piece = board[r][c]
                if piece:
                    pcolor = RED_PIECE if piece.lower() == "r" else BLACK_PIECE
                    cx = x + cell // 2
                    cy = y + cell // 2
                    radius = cell // 2 - 6
                    draw.ellipse(
                        [cx - radius, cy - radius, cx + radius, cy + radius],
                        fill=pcolor,
                        outline=(20, 20, 20, 255),
                        width=2,
                    )
                    if piece.isupper():  # king
                        kfont = self._get_font(24)
                        bbox = draw.textbbox((0, 0), "K", font=kfont)
                        tw = bbox[2] - bbox[0]
                        th = bbox[3] - bbox[1]
                        draw.text(
                            (cx - tw // 2, cy - th // 2 - 4),
                            "K",
                            fill=(255, 220, 0, 255),
                            font=kfont,
                        )
        # Coordinates
        for c in range(8):
            label = "abcdefgh"[c]
            x = margin + c * cell + cell // 2 - 6
            draw.text((x, margin + 8 * cell + 4), label, fill=TEXT_COLOR, font=font)
        for r in range(8):
            label = str(8 - r)
            y = margin + r * cell + cell // 2 - 10
            draw.text((4, y), label, fill=TEXT_COLOR, font=font)
        out = self.games_dir / "checkers.png"
        img.save(out)
        self.pending_images.append(str(out))

    # ===== 20 Questions =====

    def _tq_start(self, args: dict) -> str:
        choice = random.choice(TWENTY_Q_CONCEPTS)
        state = self._load_state()
        state["twenty_q"] = {
            "concept": choice["concept"],
            "category": choice["category"],
            "questions_asked": 0,
            "history": [],
            "started_at": datetime.now().isoformat(),
        }
        self._save_state(state)
        return (
            f"20 Questions started. You're thinking of: '{choice['concept']}' "
            f"(category: {choice['category']}). Don't tell {self.user_name} what it is. "
            f"They have 20 yes/no questions. Use twenty_q_answer to log each round."
        )

    def _tq_answer(self, args: dict) -> str:
        state = self._load_state()
        game = state.get("twenty_q")
        if not game:
            return "No active 20 Questions game. Start one with twenty_q_start."
        question = args.get("question", "").strip()
        answer = args.get("answer", "").strip()
        game["questions_asked"] += 1
        game["history"].append({"q": question, "a": answer})

        if answer in ("guess_correct", "guess_wrong"):
            won = answer == "guess_correct"
            self._save_outcome_as_memory(
                "twenty_questions",
                f"Played 20 Questions with {self.user_name}. Concept was '{game['concept']}'. "
                f"{self.user_name} {'guessed it correctly' if won else 'failed to guess'} "
                f"after {game['questions_asked']} questions."
            )
            concept = game["concept"]
            self._clear_game("twenty_q")
            return (
                f"Game over. The concept was '{concept}'. "
                f"{self.user_name} {'GOT IT' if won else 'missed it'} on question {game['questions_asked']}."
            )

        state["twenty_q"] = game
        self._save_state(state)
        remaining = 20 - game["questions_asked"]
        if remaining <= 0:
            return f"Q{game['questions_asked']}: {answer}. That was their last question — call twenty_q_reveal to end the game."
        return f"Q{game['questions_asked']}: {answer}. {remaining} questions left."

    def _tq_reveal(self, args: dict) -> str:
        state = self._load_state()
        game = state.get("twenty_q")
        if not game:
            return "No active 20 Questions game."
        concept = game["concept"]
        self._save_outcome_as_memory(
            "twenty_questions",
            f"Played 20 Questions with {self.user_name}. Concept: '{concept}'. "
            f"Game ended after {game['questions_asked']} questions."
        )
        self._clear_game("twenty_q")
        return f"Reveal: the concept was '{concept}'."

    # ===== Word Association =====

    def _wa_start(self, args: dict) -> str:
        starter = random.choice(WORD_STARTERS)
        state = self._load_state()
        state["word_association"] = {
            "chain": [{"word": starter, "whose": "system"}],
            "started_at": datetime.now().isoformat(),
        }
        self._save_state(state)
        return f"Word association started. Starter word: '{starter}'. Say the next word it triggers."

    def _wa_add(self, args: dict) -> str:
        state = self._load_state()
        game = state.get("word_association")
        if not game:
            return "No active word association chain. Start one with word_association_start."
        word = args.get("word", "").strip()
        whose = args.get("whose", "")
        if not word:
            return "Word was empty."
        # Strip down to one word
        word = word.split()[0] if word.split() else word
        game["chain"].append({"word": word, "whose": whose})
        state["word_association"] = game
        self._save_state(state)
        chain_str = " → ".join(c["word"] for c in game["chain"])
        return f"Chain ({len(game['chain'])} words): {chain_str}"

    def _wa_end(self, args: dict) -> str:
        state = self._load_state()
        game = state.get("word_association")
        if not game:
            return "No active word association chain."
        chain_str = " → ".join(c["word"] for c in game["chain"])
        self._save_outcome_as_memory(
            "word_association",
            f"Played word association with {self.user_name}. Chain ({len(game['chain'])} words): {chain_str}"
        )
        self._clear_game("word_association")
        return f"Chain saved as memory.\nFinal chain: {chain_str}"

    # ===== Would You Rather =====

    def _wyr(self, args: dict) -> str:
        return random.choice(WYR_PROMPTS)

    # --- Helpers ---

    def _get_font(self, size: int):
        for candidate in [
            "C:/Windows/Fonts/AtkinsonHyperlegible-Bold.ttf",
            "C:/Windows/Fonts/Arial.ttf",
            "C:/Windows/Fonts/comic.ttf",
        ]:
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue
        return ImageFont.load_default()
