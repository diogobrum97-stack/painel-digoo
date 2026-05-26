import os
import time
import json
import requests
import schedule
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from bling_api import buscar_cmv_bling

# ============================================================
# CONFIGURAÇÕES
# ============================================================
APP_ID     = '859327591814162'
APP_SECRET = 'OjVuDAwTDER4EUBBmdO4L1GlncBZTXmp'
SHEET_ID   = '1-1M-PCLufb2i0Vb4Gjg2R2G-qQHfjBnMHh-plMCDvrU'
MARGEM_MIN = 0.20
HORARIO    = '07:00'

COMISSAO = {
    'gold_special': 0.12,
    'gold_pro':     0.16,
    'gold':         0.13,
    'default':      0.14
}

_access_token = None
_token_expiry = 0

# ============================================================
# GOOGLE SHEETS
# ============================================================
def get_sheets_service():
    creds_json = os.environ.get('GOOGLE_CREDS_JSON')
    if not creds_json:
        raise Exception("Variável GOOGLE_CREDS_JSON não configurada!")
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    return build('sheets', 'v4', credentials=creds).spreadsheets()

# ============================================================
# TOKEN ML
# ============================================================
def get_token():
    global _access_token, _token_expiry

    if _access_token and time.time() < _token_expiry:
        return _access_token

    refresh_token = os.environ.get('ML_REFRESH_TOKEN')
    if not refresh_token:
        raise Exception("Variável ML_REFRESH_TOKEN não configurada no Railway!")

    print("🔄 Obtendo token do ML via refresh token...")
    resp = requests.post('https://api.mercadolibre.com/oauth/token', data={
        'grant_type':    'refresh_token',
        'client_id':     APP_ID,
        'client_secret': APP_SECRET,
        'refresh_token': refresh_token
    })

    if resp.status_code != 200:
        raise Exception(f"Erro ao obter token: {resp.text}")

    data          = resp.json()
    _access_token = data['access_token']
    _token_expiry = time.time() + data.get('expires_in', 21600) - 300
    print("✅ Token ML obtido!")
    return _access_token

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
        ids  = data.get('results', [])
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
            'https://api.mercadolibre.com/items',
            params={
                'ids': lote,
                'attributes': 'id,title,price,listing_type_id,available_quantity,sold_quantity,seller_custom_field'
            },
            headers={'Authorization': f'Bearer {token}'}
        )
        for entry in resp.json():
            if entry.get('code') == 200:
                detalhes.append(entry['body'])
        time.sleep(0.3)
    return detalhes

def get_visitas(item_id, user_id, token):
    try:
        hoje   = datetime.now()
        inicio = hoje - timedelta(days=30)
        fmt    = lambda d: d.strftime('%Y-%m-%d')
        resp   = requests.get(
            f'https://api.mercadolibre.com/users/{user_id}/items_visits',
            params={'ids': item_id, 'date_from': fmt(inicio), 'date_to': fmt(hoje)},
            headers={'Authorization': f'Bearer {token}'}
        )
        return resp.json().get('data', [{}])[0].get('total_visits', 0)
    except:
        return 0

# ============================================================
# CMV — busca do Bling pelo SKU do ML, fallback na planilha
# ============================================================
def ler_cmv(service, itens=None):
    cmv_map     = {}
    sku_para_id = {}

    if itens:
        skus_validos = []
        for item in itens:
            sku = item.get('seller_custom_field') or ''
            if sku:
                sku_para_id[sku] = item['id']
                skus_validos.append(sku)

        if skus_validos:
            print(f"🔍 Buscando CMV no Bling para {len(skus_validos)} SKUs...")
            bling_result = buscar_cmv_bling(skus_validos)
            for sku, custo in bling_result.items():
                item_id = sku_para_id.get(sku)
                if item_id:
                    cmv_map[item_id] = custo
            encontrados = sum(1 for v in cmv_map.values() if v > 0)
            print(f"  ✔ {encontrados}/{len(skus_validos)} produtos com CMV no Bling")
        else:
            print("⚠️  Nenhum anúncio tem SKU preenchido no ML")

    try:
        result = service.values().get(
            spreadsheetId=SHEET_ID, range='CMV!A2:C'
        ).execute()
        rows = result.get('values', [])
        for row in rows:
            if len(row) >= 3 and row[0]:
                chave = row[0].strip()
                if chave not in cmv_map or cmv_map[chave] == 0:
                    try:
                        cmv_map[chave] = float(str(row[2]).replace(',', '.'))
                    except:
                        cmv_map[chave] = 0
    except:
        pass

    return cmv_map

def garantir_abas(service):
    sheet_meta = service.get(spreadsheetId=SHEET_ID).execute()
    abas = [s['properties']['title'] for s in sheet_meta['sheets']]
    for nome in ['CMV', 'Anúncios ML']:
        if nome not in abas:
            service.batchUpdate(spreadsheetId=SHEET_ID, body={
                'requests': [{'addSheet': {'properties': {'title': nome}}}]
            }).execute()

# ============================================================
# CÁLCULO
# ============================================================
def calcular(preco, cmv, listing_type, vendas, visitas):
    comissao   = COMISSAO.get(listing_type, COMISSAO['default'])
    frete      = preco * 0.04 if preco >= 79 else 15
    custo_ml   = preco * comissao + frete
    margem_rs  = preco - cmv - custo_ml
    margem_pct = margem_rs / preco if preco > 0 else 0
    conversao  = vendas / visitas if visitas > 0 else 0

    if cmv == 0:                                                rec = '⚠️ Cadastre o CMV'
    elif margem_pct < MARGEM_MIN:                               rec = '🔴 Subir preço'
    elif margem_pct >= MARGEM_MIN and conversao > 0.03:         rec = '🟢 Pode subir'
    elif margem_pct > MARGEM_MIN + 0.10 and conversao < 0.01:  rec = '🟡 Avaliar baixar'
    else:                                                       rec = '✅ Manter'

    return {
        'comissao':   comissao,
        'custo_ml':   custo_ml,
        'margem_rs':  margem_rs,
        'margem_pct': margem_pct,
        'conversao':  conversao,
        'rec':        rec
    }

# ============================================================
# FUNÇÃO PRINCIPAL
# ============================================================
def atualizar_planilha():
    print(f"\n🔄 Atualizando — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    try:
        token   = get_token()
        user_id = get_user_id(token)
        ids     = get_anuncios(user_id, token)
        print(f"📦 {len(ids)} anúncios encontrados")
        itens   = get_detalhes(ids, token)
        service = get_sheets_service()
        garantir_abas(service)
        cmv_map = ler_cmv(service, itens=itens)

        cab = [['ID', 'Título', 'SKU', 'Modalidade', 'Preço (R$)', 'CMV (R$)',
                'Comissão ML', 'Custo ML (R$)', 'Margem (R$)', 'Margem (%)',
                'Vendas 30d', 'Estoque', 'Visitas 30d', 'Conversão (%)',
                'Recomendação', 'Atualizado em']]
        linhas = []
        for item in itens:
            sku     = item.get('seller_custom_field') or ''
            cmv     = cmv_map.get(item['id'], 0)
            visitas = get_visitas(item['id'], user_id, token)
            c       = calcular(
                item['price'], cmv,
                item.get('listing_type_id', ''),
                item.get('sold_quantity', 0),
                visitas
            )
            linhas.append([
                item['id'], item['title'], sku,
                item.get('listing_type_id', ''),
                item['price'], cmv,
                f"{c['comissao']*100:.1f}%", round(c['custo_ml'], 2),
                round(c['margem_rs'], 2), f"{c['margem_pct']*100:.1f}%",
                item.get('sold_quantity', 0), item.get('available_quantity', 0),
                visitas, f"{c['conversao']*100:.2f}%", c['rec'],
                datetime.now().strftime('%d/%m/%Y %H:%M')
            ])
            time.sleep(0.2)
            print(f"  ✔ {item['title'][:50]}")

        service.values().clear(
            spreadsheetId=SHEET_ID, range='Anúncios ML!A:P'
        ).execute()
        service.values().update(
            spreadsheetId=SHEET_ID, range='Anúncios ML!A1',
            valueInputOption='RAW', body={'values': cab + linhas}
        ).execute()
        print(f"\n✅ {len(linhas)} anúncios atualizados na planilha!")

    except Exception as e:
        print(f"❌ Erro: {e}")
        import traceback; traceback.print_exc()

# ============================================================
# INÍCIO
# ============================================================
if __name__ == '__main__':
    print("=" * 50)
    print("  DIGOO ML — Atualizador Automático")
    print("=" * 50)
    atualizar_planilha()
    schedule.every().day.at(HORARIO).do(atualizar_planilha)
    print(f"\n⏰ Agendado para todo dia às {HORARIO}")
    while True:
        schedule.run_pending()
        time.sleep(60)
