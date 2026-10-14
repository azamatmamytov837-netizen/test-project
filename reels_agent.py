#!/usr/bin/env python3
"""ИИ-агент для сценариев Reels на базе Claude API.

Запуск:
    python reels_agent.py                      # интерактивный режим
    python reels_agent.py --topic "3 ошибки новичков в зале"
    python reels_agent.py --web                # разрешить поиск трендов в интернете

Команды в чате: /save — сохранить последний сценарий, /new — новая тема, /exit — выход.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import anthropic

MODEL = "claude-opus-5-5"
BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE_DIR / "scripts"
MAX_CONTINUATIONS = 5

SYSTEM_PROMPT = """\
Ты — профессиональный сценарист коротких вертикальных видео (Instagram Reels, TikTok, YouTube Shorts).
Твоя задача — писать готовые к съёмке сценарии, которые удерживают внимание и ведут к цели автора.

Как ты работаешь:
- Опирайся на профиль автора ниже: ниша, аудитория, тон, цель, формат съёмки, оффер, запреты.
- Если тема не указана или слишком размыта — задай 1–3 коротких уточняющих вопроса, затем пиши сценарий.
- Первые 1–3 секунды решают всё: хук должен создавать интригу, конфликт или обещание пользы.
- Пиши разговорным языком, короткими фразами — так, как автор реально скажет это в камеру.
- Укладывайся в заданную длительность: ориентир ~2–2,5 слова озвучки на секунду.
- Не выдумывай факты, цифры, исследования и цитаты. Если утверждение требует проверки — пометь его
  «[проверить]». Если пользуешься веб-поиском — укажи ссылки на источники в конце.
- Не обещай гарантированных результатов (охваты, деньги, здоровье) и не давай медицинских/финансовых
  рекомендаций как истину.

Формат ответа со сценарием (Markdown):

## 🎬 <Рабочее название>
**Длительность:** N сек · **Формат:** … · **Цель:** …

### Хук (0–3 сек) — 3 варианта
1. …
2. …
3. …

### Раскадровка
| Время | В кадре (что снимать) | Текст за кадром / в камеру | Текст на экране |
|---|---|---|---|
| 0–3 с | … | … | … |

### Призыв к действию (CTA)
…

### Подпись к посту
…

### Хэштеги
5–10 релевантных хэштегов.

### Советы к съёмке и монтажу
Музыка/звук, темп склеек, обложка — 3–5 пунктов.

После сценария коротко предложи 2–3 варианта, как его можно усилить или переделать.
Если пользователь просит правки — переписывай сценарий целиком в том же формате.
"""


def load_profile(path: Path) -> str:
    if not path.exists():
        return "Профиль автора не задан. Уточни у пользователя нишу, аудиторию и цель, если это важно для темы."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"Ошибка в файле профиля {path}: {e}")
    lines = [f"- {key}: {value}" for key, value in data.items() if str(value).strip()]
    return "Профиль автора:\n" + "\n".join(lines)


class ReelsAgent:
    def __init__(self, profile_text: str, use_web: bool, effort: str):
        self.client = anthropic.Anthropic()
        self.system = SYSTEM_PROMPT + "\n" + profile_text
        self.effort = effort
        self.tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}] if use_web else []
        self.messages: list = []
        self.last_answer = ""

    def reset(self) -> None:
        self.messages = []
        self.last_answer = ""

    def _request(self):
        kwargs = dict(
            model=MODEL,
            max_tokens=64000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=self.system,
            cache_control={"type": "ephemeral"},
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            messages=self.messages,
        )
        if self.tools:
            kwargs["tools"] = self.tools
        with self.client.beta.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
            return stream.get_final_message()

    def ask(self, user_text: str) -> str:
        start = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        try:
            return self._run_turn()
        except Exception:
            del self.messages[start:]  # при ошибке откатываем незавершённый ход
            raise

    def _run_turn(self) -> str:
        parts = []
        for _ in range(MAX_CONTINUATIONS):
            response = self._request()
            # Сохраняем ответ целиком (включая thinking и результаты поиска) — история только дописывается.
            self.messages.append({"role": "assistant", "content": response.content})
            parts.extend(b.text for b in response.content if b.type == "text")
            if response.stop_reason == "refusal":
                print("\n[Модель отказалась отвечать на этот запрос. Переформулируйте тему.]")
                break
            if response.stop_reason == "max_tokens":
                print("\n[Ответ обрезан по лимиту длины.]")
                break
            if response.stop_reason != "pause_turn":
                break
            # pause_turn: сервер прервал долгий поиск — повторяем запрос, он продолжит сам.
        print()
        self.last_answer = "".join(parts)
        return self.last_answer


def save_script(text: str) -> Path:
    SCRIPTS_DIR.mkdir(exist_ok=True)
    path = SCRIPTS_DIR / f"reels_{datetime.now():%Y-%m-%d_%H-%M-%S}.md"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="ИИ-агент для сценариев Reels")
    parser.add_argument("--topic", help="тема рилс (иначе спросит в чате)")
    parser.add_argument("--profile", default=str(BASE_DIR / "profile.json"), help="путь к профилю автора (JSON)")
    parser.add_argument("--web", action="store_true", help="разрешить веб-поиск трендов и фактов")
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                        help="глубина проработки (выше — дольше и дороже)")
    args = parser.parse_args()

    agent = ReelsAgent(load_profile(Path(args.profile)), use_web=args.web, effort=args.effort)
    print("🎬 Агент сценариев Reels. Команды: /save, /new, /exit\n")

    pending = args.topic
    while True:
        if pending:
            user_text, pending = pending, None
            print(f"Вы: {user_text}")
        else:
            try:
                user_text = input("Вы: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
        if not user_text:
            continue
        if user_text == "/exit":
            break
        if user_text == "/new":
            agent.reset()
            print("Начинаем новую тему.\n")
            continue
        if user_text == "/save":
            if agent.last_answer:
                print(f"Сохранено: {save_script(agent.last_answer)}\n")
            else:
                print("Пока нечего сохранять.\n")
            continue

        print("\nАгент: ", end="")
        try:
            agent.ask(user_text)
        except anthropic.AuthenticationError:
            sys.exit("Неверный или отсутствующий ключ. Задайте переменную окружения ANTHROPIC_API_KEY.")
        except anthropic.RateLimitError:
            print("\n[Превышен лимит запросов, попробуйте через минуту.]")
        except anthropic.APIStatusError as e:
            print(f"\n[Ошибка API {e.status_code}: {e.message}]")
        except anthropic.APIConnectionError:
            print("\n[Нет соединения с API. Проверьте интернет.]")
        print()


if __name__ == "__main__":
    main()
