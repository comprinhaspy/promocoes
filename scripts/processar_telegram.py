"""
Script que roda periodicamente (via GitHub Actions).
1. Busca mensagens novas no bot do Telegram.
2. Para cada mensagem com foto + legenda, manda o texto pra uma IA (Gemini)
   que devolve um título, preço e descrição de venda prontos.
3. Baixa a foto e salva em docs/fotos/.
4. Adiciona a nova promoção em docs/promocoes.json.
5. Guarda até onde já leu, pra não repetir mensagem em futuras execuções.

Não precisa mexer neste arquivo. As únicas coisas configuráveis são as
variáveis de ambiente TELEGRAM_BOT_TOKEN e GEMINI_API_KEY (ficam nos
"Secrets" do repositório no GitHub, nunca escritas aqui).
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "docs"
FOTOS_DIR = SITE_DIR / "fotos"
PROMOCOES_PATH = SITE_DIR / "promocoes.json"
OFFSET_PATH = ROOT / "data" / "offset.txt"

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}"
GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)


def http_get_json(url, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", errors="replace")
        print(f"Erro HTTP {e.code} chamando {url}:", corpo)
        raise


def carregar_offset():
    if OFFSET_PATH.exists():
        conteudo = OFFSET_PATH.read_text().strip()
        if conteudo.isdigit():
            return int(conteudo)
    return 0


def salvar_offset(offset):
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset))


def buscar_atualizacoes(offset):
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset:
        params["offset"] = offset
    resp = http_get_json(f"{TELEGRAM_API}/getUpdates", params)
    if not resp.get("ok"):
        print("Erro ao buscar updates:", resp)
        return []
    return resp.get("result", [])


def baixar_foto(file_id, destino):
    info = http_get_json(f"{TELEGRAM_API}/getFile", {"file_id": file_id})
    if not info.get("ok"):
        return False
    file_path = info["result"]["file_path"]
    url = f"{TELEGRAM_FILE_API}/{file_path}"
    urllib.request.urlretrieve(url, destino)
    return True


def enviar_mensagem(chat_id, texto):
    """Manda uma mensagem de volta pro chat do Telegram (confirmação ou aviso
    de erro). Se isso falhar por qualquer motivo, só registra no log e segue
    em frente — nunca deve derrubar o processamento das promoções."""
    if not chat_id:
        return
    try:
        http_post_json(
            f"{TELEGRAM_API}/sendMessage",
            {"chat_id": chat_id, "text": texto},
        )
    except Exception as e:
        print(f"Não consegui responder no Telegram: {e}")


def gerar_texto_promocao(legenda):
    """Manda a legenda que o usuário digitou pro Gemini e pede de volta
    um JSON estruturado com titulo, preco e descricao de venda."""

    prompt = f"""Você escreve anúncios curtos e atrativos para promoções de
produtos importados vendidos no Brasil, a partir do texto que um
lojista digitou no Telegram.

Regras importantes:
- Use APENAS as informações que estão no texto abaixo. Nunca invente
  preço, marca, tamanho ou qualquer dado que não esteja escrito.
- Se não houver um preço claro no texto, deixe o campo "preco" como "Consulte".
- O "titulo" deve ter no máximo 6 palavras.
- A "descricao" deve ter 1 a 2 frases curtas, tom vendedor mas honesto,
  sem emojis, em português do Brasil.
- O campo "preco" deve vir formatado como "R$ 000,00" quando houver valor.

Texto digitado pelo lojista:
\"\"\"{legenda}\"\"\"

Responda SOMENTE com um JSON no formato:
{{"titulo": "...", "preco": "...", "descricao": "..."}}"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "titulo": {"type": "STRING"},
                    "preco": {"type": "STRING"},
                    "descricao": {"type": "STRING"},
                },
                "required": ["titulo", "preco", "descricao"],
            },
        },
    }

    resp = http_post_json(GEMINI_URL, payload)
    try:
        texto = resp["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(texto)
    except Exception as e:
        print("Falha ao interpretar resposta da IA:", e, resp)
        return None


def carregar_promocoes():
    if PROMOCOES_PATH.exists():
        try:
            return json.loads(PROMOCOES_PATH.read_text())
        except json.JSONDecodeError:
            return []
    return []


def salvar_promocoes(lista):
    PROMOCOES_PATH.write_text(json.dumps(lista, ensure_ascii=False, indent=2))


def main():
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        print("Faltam as variáveis TELEGRAM_BOT_TOKEN e/ou GEMINI_API_KEY.")
        sys.exit(1)

    FOTOS_DIR.mkdir(parents=True, exist_ok=True)
    offset = carregar_offset()
    updates = buscar_atualizacoes(offset)

    if not updates:
        print("Nenhuma mensagem nova.")
        return

    promocoes = carregar_promocoes()
    maior_update_id = offset

    try:
        for update in updates:
            # Marca a mensagem como "vista" mesmo que dê erro nela, pra não
            # ficar tentando a mesma mensagem quebrada pra sempre.
            maior_update_id = max(maior_update_id, update["update_id"] + 1)
            msg = update.get("message")
            if not msg:
                continue

            try:
                legenda = msg.get("caption") or msg.get("text") or ""
                if not legenda.strip():
                    continue

                imagem_relativa = ""
                fotos = msg.get("photo")
                if fotos:
                    maior_foto = fotos[-1]
                    file_id = maior_foto["file_id"]
                    nome_arquivo = f"{msg['message_id']}.jpg"
                    destino = FOTOS_DIR / nome_arquivo
                    if baixar_foto(file_id, destino):
                        imagem_relativa = f"fotos/{nome_arquivo}"

                gerado = gerar_texto_promocao(legenda)
                if not gerado:
                    continue

                nova_promocao = {
                    "id": msg["message_id"],
                    "data": time.strftime(
                        "%Y-%m-%dT%H:%M:%S", time.gmtime(msg.get("date", time.time()))
                    ),
                    "titulo": gerado.get("titulo", "Promoção"),
                    "preco": gerado.get("preco", "Consulte"),
                    "descricao": gerado.get("descricao", legenda[:200]),
                    "imagem": imagem_relativa,
                }
                promocoes.append(nova_promocao)
                print("Adicionada:", nova_promocao["titulo"])
                enviar_mensagem(
                    msg["chat"]["id"],
                    f"✅ Promoção publicada no site: {nova_promocao['titulo']} "
                    f"— {nova_promocao['preco']}",
                )
            except Exception as e:
                # Não deixa uma mensagem com problema derrubar as outras.
                print(f"Falhou ao processar a mensagem {msg.get('message_id')}: {e}")
                enviar_mensagem(
                    msg.get("chat", {}).get("id"),
                    "⚠️ Não consegui publicar essa promoção agora. "
                    "Vou tentar de novo automaticamente em alguns minutos.",
                )
                continue
    finally:
        # Salva o que já foi processado com sucesso, mesmo que algo tenha
        # dado errado no meio do caminho.
        salvar_promocoes(promocoes)
        salvar_offset(maior_update_id)


if __name__ == "__main__":
    main()
