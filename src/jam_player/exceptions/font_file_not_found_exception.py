from jam_player.exceptions import jam_player_exception


class FontFileNotFoundException(jam_player_exception.JamPlayerException):

    def __init__(self, font_path: str, message: str = None):
        self.message = f"Could not load font file from {font_path}."
        if message:
            self.message = f"{self.message} {message}"
        super().__init__(self.message)
