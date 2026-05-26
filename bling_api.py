import os
import time
import requests
import base64

_bling_token  = None
_bling_expiry = 0


def _get_credentials():
    client_id     = os.environ.get('BLING_CLIENT_ID')
    client_secret = os.environ.get('BLING_CLIENT_SECRET')
    refresh_token = os.environ.get('BLING_REFRESH_TOKEN')
    if not all([client_id, client_secret, refresh_token]):
        raise Exception(
            "Variáveis BLING_CLIENT_ID, BLING_CLIENT_SECRET ou "
            "BLING_REFRESH_TOKEN não configuradas!"
        )
    return client_id, client_secret, refresh_token


def get_bling_token():
    global _bling_token, _bling_expiry

    if _bling_token and time.time() < _bling_expiry:
        return _bling_token

    client_id, client_secret, refresh_token = _get_credentials()
    credentials = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()

    print("🔄 Renovando token do Bling...")
    resp = requests.post(
        "https://www.bling.com.br/Api/v3/oauth/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token
        }
    )

    if resp.status_code != 200:
        raise Exception(f"Erro ao renovar token Bling: {resp.text}")

    data          = resp.json()
    _bling_token  = data['access_token']
    _bling_expiry = time.time() + data.get('expires_in', 21600) - 300

    os.environ['BLING_REFRESH_TOKEN'] = data['refresh_token']
    print("✅ Token Bling renovado!")
    return _bling_token


def _extrair_custo(produto):
    """Extrai o melhor custo disponível de um objeto produto/variação."""
    custo_medio = produto.get('custoMedio')
    preco_custo = produto.get('precoCusto')
    if custo_medio is not None and float(str(custo_medio).replace(',', '.') or 0) != 0:
        return float(str(custo_medio).replace(',', '.'))
    if preco_custo is not None and float(str(preco_custo).replace(',', '.') or 0) != 0:
        return float(str(preco_custo).replace(',', '.'))
    return 0


def _buscar_custo_por_sku(sku, token):
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Busca produto pelo código (funciona para produtos simples)
    resp = requests.get(
        "https://www.bling.com.br/Api/v3/produtos",
        headers=headers,
        params={"codigo": sku}
    )
    if resp.status_code != 200:
        return 0

    data = resp.json().get('data', [])
    if not data:
        return 0

    produto = data[0]
    produto_id = produto.get('id')

    # 2. Tenta custo direto do produto
    custo = _extrair_custo(produto)
    if custo != 0:
        return custo

    # 3. Se custo=0, busca nas variações do produto pai
    if produto_id:
        time.sleep(0.2)
        resp_var = requests.get(
            f"https://www.bling.com.br/Api/v3/produtos/{produto_id}/variacoes",
            headers=headers
        )
        if resp_var.status_code == 200:
            variacoes = resp_var.json().get('data', [])
            for var in variacoes:
                # Verifica se o código da variação bate com o SKU buscado
                if var.get('codigo') == sku:
                    custo = _extrair_custo(var)
                    if custo != 0:
                        return custo

    return 0


def buscar_cmv_bling(skus: list) -> dict:
    try:
        token = get_bling_token()
    except Exception as e:
        print(f"⚠️  Bling indisponível: {e}")
        return {}

    cmv_map = {}
    for sku in skus:
        if not sku:
            continue
        try:
            cmv_map[sku] = _buscar_custo_por_sku(sku, token)
            time.sleep(0.3)
        except Exception as e:
            print(f"⚠️  Erro ao buscar SKU {sku}: {e}")
            cmv_map[sku] = 0

    return cmv_map
