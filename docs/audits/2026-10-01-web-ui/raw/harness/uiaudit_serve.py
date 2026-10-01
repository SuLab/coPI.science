import os, sys
os.environ.update(BASE_URL="http://localhost:8790", ENVIRONMENT="development",
                  ALLOW_HTTP_SESSIONS="true", SECRET_KEY="uiaudit-local-only", ANTHROPIC_API_KEY="")
assert os.environ["DATABASE_URL"].endswith("/copi_e2e")
sys.path.insert(0, os.getcwd())
import uvicorn
from src.main import create_app
from src.services import assessment_chat
from tests.assessment_chat_support import PITCH_TEXT, RECORD_URL, citation
from tests.fakes import ChatScript, FakeAsyncAnthropic

def script():
    first = ("**Answer.** <img src=x onerror=\"window.__xss=1\"> [js](javascript:window.__xss=1) "
             "![pixel](https://attacker.example/p.png) " + "x" * 200 + f" The record cites {RECORD_URL}.")
    return ChatScript(delay=1.0, segments=[(first, [citation(1, 0, PITCH_TEXT)])])
class _Many(FakeAsyncAnthropic):
    pass
fake = FakeAsyncAnthropic([script() for _ in range(50)])
assessment_chat.get_async_anthropic_client = lambda: fake
uvicorn.run(create_app(), host="127.0.0.1", port=8790, log_level="warning")
