import os
import time
import json
import requests
import schedule
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ============================================================
# CONFIGURAÇÕES
# ============================================================
APP_ID          = '859327591814162'
APP_SECRET      = 'OjVuDAwTDER4EUBBmdO4L1GlncBZTXmp'
SHEET_ID        = '1-1M-PCLufb2i0Vb4Gjg2R2G-qQHfjBnMHh-plMCDvrU'
MARGEM_MIN      = 0.20
HORARIO         = '07:00'  # Horário de atualização automática

COMISSAO = {
    'gold_special': 0.12,
    'gold_pro':     0.16,
    'gold':         0.13,
    'default':      0.14
}

TOKEN_FILE = 'ml_token.json'
CREDS_FILE = 'google_creds.json'

# ============================================================
# AUTENTICAÇÃO MERCADO LIVRE
# ============================================================
def salvar_token(data):
    data['expires_at'] = time.time() + data.get('expires_in', 21600) - 60
    with open(TOKEN_FILE, 'w') as f:
        json.dump(data, f)

def carregar_token():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, 'r') as f:
        return json.load(f)

def get_token():
    token_data = carregar_token()

    # Token ainda válido
    if token_data and time.time() < token_data.get('expires_at', 0):
        return token_data['access_token']

    # Tenta refresh
    if token_data and token_data.get('refresh_token'):
        print("🔄 Renovando token do ML...")
        resp = requests.post('https://api.mercadolibre.com/oauth/token', data={
            'grant_type':    'refresh_token',
            'client_id':     APP_ID,
            'client_secret': APP_SECRET,
            'refresh_token': token_data['refresh_token']
        })
        if resp.status_code == 200:
            data = resp.json()
            salvar_token(data)
            print("✅ Token renovado!")
            return data['access_token']

    # Precisa autorizar do zero
    print("\n⚠️  Token não encontrado ou expirado. Iniciando autorização...")
    autorizar()
    token_data = carregar_token()
    return token_data['access_token'] if token_data else None

def autorizar():
    redirect = 'https://script.google.com/'
    url = f'https://auth.mercadolivre.com.br/authorization?response_type=code&client_id={APP_ID}&redirect_uri={requests.utils.quote(redirect)}'
    print(f"\n1. Abra este link no navegador:\n{url}\n")
    print("2. Faça login no Mercado Livre")
    print("3. Após redirecionar, copie o código da URL após '?code='")
    print("   Exemplo: https://script.google.com/?code=TG-XXXXXXXX")
    print("            copie só: TG-XXXXXXXX\n")
    code = input("Cole o código aqui: ").strip()

    resp = requests.post('https://api.mercadolibre.com/oauth/token', data={
        'grant_type':   'authorization_code',
        'client_id':    APP_ID,
        'client_secret': APP_SECRET,
        'code':         code,
        'redirect_uri': redirect
    })
    if resp.status_code == 200:
        salvar_token(resp.json())
        print("✅ Autorizado com sucesso!\n")
    else:
        print(f"❌ Erro: {resp.text}")
        exit(1)

# ============================================================
# GOOGLE SHEETS
# ============================================================
def get_sheets_service():
    creds = Credentials.from_service_account_file(
        CREDS_FILE,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    return build('sheets', 'v4', credentials=creds).spreadsheets()

def ler_cmv(service):
    try:
        result = service.values().get(
            spreadsheetId=SHEET_ID,
            range='CMV!A2:C'
        ).execute()
        rows = result.get('values', [])
        cmv_map = {}
        for row in rows:
            if len(row) >= 3 and row[0]:
                try:
                    cmv_map[row[0].strip()] = float(str(row[2]).replace(',', '.'))
                except:
                    cmv_map[row[0].strip()] = 0
        return cmv_map
    except:
        return {}

def garantir_aba_cmv(service):
    sheet_meta = service.get(spreadsheetId=SHEET_ID).execute()
    abas = [s['properties']['title'] for s in sheet_meta['sheets']]
    if 'CMV' not in abas:
        service.batchUpdate(spreadsheetId=SHEET_ID, body={
            'requests': [{'addSheet': {'properties': {'title': 'CMV'}}}]
        }).execute()
        service.values().update(
            spreadsheetId=SHEET_ID,
            range='CMV!A1:C1',
            valueInputOption='RAW',
            body={'values': [['ID do Anúncio', 'Nome', 'CMV (R$)']]}
        ).execute()
        print("✅ Aba CMV criada!")

# ============================================================
# API MERCADO LIVRE
# ============================================================
def get_user_id(token):
    resp = requests.get('https://api.mercadolibre.com/users/me',
                        headers={'Authorization': f'Bearer {token}'})
    return resp.json()['id']

def get_anuncios(user_id, token):
    items, offset = [], 0
    while True:
        resp = requests.get(
            f'https://api.mercadolibre.com/users/{user_id}/items/search',
            params={'status': 'active', 'offset': offset, 'limit': 50},
            headers={'Authorization': f'Bearer {token}'}
        )
        data = resp.json()
        ids = data.get('results', [])
        if not ids:
            break
        items.extend(ids)
        offset += 50
        if offset >= data.get('paging', {}).get('total', 0):
            break
        time.sleep(0.3)
    return items

def get_detalhes(ids, token):
    detalhes = []
    for i in range(0, len(ids), 20):
        lote = ','.join(ids[i:i+20])
        resp = requests.get(
            f'https://api.mercadolibre.com/items',
            params={'ids': lote, 'attributes': 'id,title,price,listing_type_id,available_quantity,sold_quantity'},
            headers={'Authorization': f'Bearer {token}'}
        )
        for entry in resp.json():
            if entry.get('code') == 200:
                detalhes.append(entry['body'])
        time.sleep(0.3)
    return detalhes

def get_visitas(item_id, user_id, token):
    try:
        hoje = datetime.now()
        inicio = hoje - timedelta(days=30)
        fmt = lambda d: d.strftime('%Y-%m-%d')
        resp = requests.get(
            f'https://api.mercadolibre.com/users/{user_id}/items_visits',
            params={'ids': item_id, 'date_from': fmt(inicio), 'date_to': fmt(hoje)},
            headers={'Authorization': f'Bearer {token}'}
        )
        return resp.json().get('data', [{}])[0].get('total_visits', 0)
    except:
        return 0

# ============================================================
# CÁLCULO DE MARGEM E RECOMENDAÇÃO
# ============================================================
def calcular(preco, cmv, listing_type, vendas, visitas):
    comissao = COMISSAO.get(listing_type, COMISSAO['default'])
    frete = preco * 0.04 if preco >= 79 else 15
    custo_ml = preco * comissao + frete
    margem_rs = preco - cmv - custo_ml
    margem_pct = margem_rs / preco if preco > 0 else 0
    conversao = vendas / visitas if visitas > 0 else 0

    if cmv == 0:
        rec = '⚠️ Cadastre o CMV'
    elif margem_pct < MARGEM_MIN:
        rec = '🔴 Subir preço'
    elif margem_pct >= MARGEM_MIN and conversao > 0.03:
        rec = '🟢 Pode subir'
    elif margem_pct > MARGEM_MIN + 0.10 and conversao < 0.01:
        rec = '🟡 Avaliar baixar'
    else:
        rec = '✅ Manter'

    return {
        'comissao': comissao,
        'custo_ml': custo_ml,
        'margem_rs': margem_rs,
        'margem_pct': margem_pct,
        'conversao': conversao,
        'rec': rec
    }

# ============================================================
# FUNÇÃO PRINCIPAL
# ============================================================
def atualizar_planilha():
    print(f"\n🔄 Iniciando atualização — {datetime.now().strftime('%d/%m/%Y %H:%M')}")

    token = get_token()
    if not token:
        print("❌ Sem token. Abortando.")
        return

    try:
        user_id = get_user_id(token)
        print(f"✅ Usuário ML: {user_id}")

        ids = get_anuncios(user_id, token)
        print(f"📦 {len(ids)} anúncios ativos encontrados")

        itens = get_detalhes(ids, token)
        service = get_sheets_service()
        garantir_aba_cmv(service)
        cmv_map = ler_cmv(service)

        cabecalho = [[
            'ID', 'Título', 'Modalidade', 'Preço (R$)', 'CMV (R$)',
            'Comissão ML', 'Custo ML (R$)', 'Margem (R$)', 'Margem (%)',
            'Vendas 30d', 'Estoque', 'Visitas 30d', 'Conversão (%)',
            'Recomendação', 'Atualizado em'
        ]]

        linhas = []
        for item in itens:
            cmv = cmv_map.get(item['id'], 0)
            visitas = get_visitas(item['id'], user_id, token)
            c = calcular(item['price'], cmv, item.get('listing_type_id', ''), item.get('sold_quantity', 0), visitas)

            linhas.append([
                item['id'],
                item['title'],
                item.get('listing_type_id', ''),
                item['price'],
                cmv,
                f"{c['comissao']*100:.1f}%",
                round(c['custo_ml'], 2),
                round(c['margem_rs'], 2),
                f"{c['margem_pct']*100:.1f}%",
                item.get('sold_quantity', 0),
                item.get('available_quantity', 0),
                visitas,
                f"{c['conversao']*100:.2f}%",
                c['rec'],
                datetime.now().strftime('%d/%m/%Y %H:%M')
            ])
            time.sleep(0.2)
            print(f"  ✔ {item['title'][:50]}")

        # Atualiza aba "Anúncios ML"
        sheet_meta = service.get(spreadsheetId=SHEET_ID).execute()
        abas = [s['properties']['title'] for s in sheet_meta['sheets']]
        if 'Anúncios ML' not in abas:
            service.batchUpdate(spreadsheetId=SHEET_ID, body={
                'requests': [{'addSheet': {'properties': {'title': 'Anúncios ML'}}}]
            }).execute()

        # Limpa e escreve
        service.values().clear(
            spreadsheetId=SHEET_ID,
            range='Anúncios ML!A:O'
        ).execute()

        service.values().update(
            spreadsheetId=SHEET_ID,
            range='Anúncios ML!A1',
            valueInputOption='RAW',
            body={'values': cabecalho + linhas}
        ).execute()

        print(f"\n✅ Planilha atualizada! {len(linhas)} anúncios.")

    except Exception as e:
        print(f"❌ Erro: {e}")
        import traceback
        traceback.print_exc()

# ============================================================
# AGENDAMENTO AUTOMÁTICO
# ============================================================
if __name__ == '__main__':
    print("=" * 50)
    print("  DIGOO ML — Atualizador de Planilha")
    print("=" * 50)

    # Primeira execução: autoriza se necessário
    get_token()

    # Roda imediatamente na primeira vez
    atualizar_planilha()

    # Agenda para todo dia no horário definido
    schedule.every().day.at(HORARIO).do(atualizar_planilha)
    print(f"\n⏰ Agendado para rodar todo dia às {HORARIO}")
    print("   Deixe este processo rodando em segundo plano.\n")

    while True:
        schedule.run_pending()
        time.sleep(60)
