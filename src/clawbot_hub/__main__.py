"""`python -m clawbot_hub`：和 `hubctl` 是同一个命令行。"""

from clawbot_hub.cli import app

if __name__ == "__main__":
    app(prog_name="hubctl")
