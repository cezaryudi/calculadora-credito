import sys
import streamlit as st
import pandas as pd
import numpy as np
import numpy_financial as npf
from scipy.optimize import fsolve
from scipy.interpolate import interp1d
import requests
from datetime import datetime
from dateutil.relativedelta import relativedelta
import warnings

warnings.filterwarnings('ignore', category=RuntimeWarning)

# ==========================================
# TRAVA DE SEGURANÇA PARA INICIAÇÃO CORRETA
# ==========================================
if not st.runtime.exists():
    print("\n" + "="*80)
    print(" ⚠️ ATENÇÃO: O servidor da calculadora não foi iniciado corretamente.")
    print(" Este script precisa ser rodado pelo Streamlit para abrir o navegador.")
    print(" Por favor, copie e cole o comando exato abaixo no seu terminal/PowerShell:")
    print("-" * 80)
    print(r"python.exe -m streamlit run C:\Users\cezar.yudi_contasimp\Desktop\Python\Calculadora\Super_Calculadora.py")
    print("-" * 80)
    print("="*80 + "\n")
    sys.exit(1)

# ==========================================
# INTEGRAÇÃO B3 E CACHE
# ==========================================
@st.cache_data(ttl=3600)
def obter_curva_b3_limpa():
    meses_b3 = {'F': 1, 'G': 2, 'H': 3, 'J': 4, 'K': 5, 'M': 6,
                'N': 7, 'Q': 8, 'U': 9, 'V': 10, 'X': 11, 'Z': 12}
    url = "https://cotacao.b3.com.br/mds/api/v1/DerivativeQuotation/DI1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.b3.com.br/",
        "Origin": "https://www.b3.com.br"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None
        contratos = response.json().get('Scty', [])
        hoje = datetime.now()
        dados_limpos = []
        for contrato in contratos:
            ticker = contrato.get('symb', '')
            taxa = contrato.get('SctyQtn', {}).get('curPrc', 0)
            if taxa > 0 and len(ticker) >= 6:
                letra_mes = ticker[3]
                ano_contrato = int("20" + ticker[4:6])
                if letra_mes in meses_b3:
                    mes_contrato = meses_b3[letra_mes]
                    meses_a_frente = (ano_contrato - hoje.year) * 12 + (mes_contrato - hoje.month)
                    if meses_a_frente > 0:
                        dados_limpos.append({'mes': meses_a_frente, 'taxa': taxa / 100})
        df = pd.DataFrame(dados_limpos).sort_values('mes').drop_duplicates(subset=['mes'], keep='first')
        return df if not df.empty else None
    except:
        return None

def gerar_curva_60_meses_b3(df_curva_b3):
    if df_curva_b3 is None or df_curva_b3.empty:
        return np.array([0.1050] * 60)
    m, t = df_curva_b3['mes'].values, df_curva_b3['taxa'].values
    if len(m) < 2:
        return np.array([t[0] if len(t) > 0 else 0.1050] * 60)
    if m[0] > 1:
        m, t = np.insert(m, 0, 1), np.insert(t, 0, t[0])
    
    interp = interp1d(m, t, kind='linear', bounds_error=False, fill_value=(t[0], t[-1]))
    curva = interp(np.arange(1, 61))
    curva_limpa = np.nan_to_num(curva, nan=t[-1])
    return curva_limpa

# ==========================================
# MOTOR MATEMÁTICO (Baseado em Dias Exatos)
# ==========================================
class MotorPricerAnalitico:
    def __init__(self, curva_cdi, spread_aa, pct_capital, opex_pct, tac_fixa):
        self.curva_cdi = curva_cdi
        self.spread_aa = spread_aa
        self.pct_capital = pct_capital
        self.opex_pct = opex_pct
        self.tac_fixa = tac_fixa

    def gerar_cronograma_dias(self, dt_emissao, dt_1_venc, parcelas):
        datas = [pd.to_datetime(dt_emissao)]
        dt_atual = pd.to_datetime(dt_1_venc)
        for _ in range(parcelas):
            datas.append(dt_atual)
            dt_atual = dt_atual + relativedelta(months=1)
            
        dc_acumulados = [(d - datas[0]).days for d in datas]
        dc_periodo = [0] + [(datas[i] - datas[i-1]).days for i in range(1, len(datas))]
        du_periodo = [0] + [np.busday_count(datas[i-1].date(), datas[i].date()) for i in range(1, len(datas))]
        return datas, dc_acumulados, dc_periodo, du_periodo

    def calcular_iof_exato(self, valor_bruto, taxa_am, dt_emissao, dt_1_venc, parcelas, tipo_pagamento, is_isento_iof):
        if is_isento_iof: return 0.0

        _, dc_acumulados, _, _ = self.gerar_cronograma_dias(dt_emissao, dt_1_venc, parcelas)
        iof_total = valor_bruto * 0.0038 
        saldo = valor_bruto
        
        pmt = npf.pmt(taxa_am, parcelas, -valor_bruto) if taxa_am > 0 else valor_bruto / parcelas
        amort_constante = valor_bruto / parcelas

        for i in range(1, parcelas + 1):
            dias_corridos_total = dc_acumulados[i]
            iof_diario = min(dias_corridos_total * 0.000082, 0.02993) 
            
            if tipo_pagamento in ['DESCONTO', 'BULLET', 'AMERICANO']:
                amort = valor_bruto if i == parcelas else 0
            elif tipo_pagamento == 'SAC':
                amort = amort_constante
            else: 
                juros = saldo * taxa_am 
                amort = pmt - juros if i < parcelas else saldo
                
            iof_total += amort * iof_diario
            saldo -= amort
            
        return iof_total

    def resolver_v_bruto(self, v_liquido_alvo, taxa_am, dt_emissao, dt_1_venc, parcelas, tipo_pagamento, is_isento_iof):
        v_bruto = v_liquido_alvo + self.tac_fixa 
        for _ in range(30):
            iof = self.calcular_iof_exato(v_bruto, taxa_am, dt_emissao, dt_1_venc, parcelas, tipo_pagamento, is_isento_iof)
            juros_upfront = v_bruto * taxa_am * parcelas if tipo_pagamento == 'DESCONTO' else 0.0
            
            v_liquido_calc = v_bruto - iof - self.tac_fixa - juros_upfront
            erro = v_liquido_alvo - v_liquido_calc
            if abs(erro) < 0.01: break
            v_bruto += erro 
        return v_bruto

    def calcular_dre_operacao(self, taxa_am, valor_input, dt_emissao, dt_1_venc, parcelas, base_calc, pd_aa, lgd, tipo_pagamento, is_financiado, is_isento_iof):
        datas, dc_acumulados, dc_periodo, du_periodo = self.gerar_cronograma_dias(dt_emissao, dt_1_venc, parcelas)
        
        if is_financiado:
            valor_bruto = self.resolver_v_bruto(valor_input, taxa_am, dt_emissao, dt_1_venc, parcelas, tipo_pagamento, is_isento_iof)
            valor_liquido = valor_input
        else:
            valor_bruto = valor_input
            iof = self.calcular_iof_exato(valor_bruto, taxa_am, dt_emissao, dt_1_venc, parcelas, tipo_pagamento, is_isento_iof)
            juros_upfront = valor_bruto * taxa_am * (dc_acumulados[-1]/30) if tipo_pagamento == 'DESCONTO' else 0.0
            valor_liquido = valor_bruto - iof - self.tac_fixa - juros_upfront

        iof_final = self.calcular_iof_exato(valor_bruto, taxa_am, dt_emissao, dt_1_venc, parcelas, tipo_pagamento, is_isento_iof)
        juros_upfront = valor_bruto * taxa_am * (dc_acumulados[-1]/30) if tipo_pagamento == 'DESCONTO' else 0.0
        
        saldo = valor_bruto
        pmt = npf.pmt(taxa_am, parcelas, -valor_bruto) if taxa_am > 0 else valor_bruto / parcelas
        amort_constante = valor_bruto / parcelas

        total_juros = juros_upfront 
        total_funding = 0
        total_ecl = 0
        
        pd_diaria = (pd_aa / 360) 
        
        cronograma = []
        cronograma.append({
            'Parcela': 0, 'Data': datas[0].strftime('%d/%m/%Y'), 'Dias Período': 0,
            'Saldo Inicial': 0.0, 'Amortização': 0.0, 'Juros da Parcela': juros_upfront,
            'Prestação (Cliente)': juros_upfront, 'Custo Funding': 0.0, 'Perda Esperada (ECL)': 0.0,
            'Saldo Final': valor_bruto
        })

        for i in range(1, parcelas + 1):
            saldo_inicial = saldo
            
            if tipo_pagamento == 'DESCONTO':
                juros = 0
                amort = valor_bruto if i == parcelas else 0
            elif tipo_pagamento == 'BULLET':
                juros = (valor_bruto * ((1 + taxa_am)**(dc_acumulados[-1]/30) - 1)) if i == parcelas else 0
                amort = valor_bruto if i == parcelas else 0
            elif tipo_pagamento == 'AMERICANO': 
                juros = saldo * taxa_am
                amort = valor_bruto if i == parcelas else 0
            elif tipo_pagamento == 'SAC':
                juros = saldo * taxa_am
                amort = amort_constante
            else: # PRICE
                juros = saldo * taxa_am
                amort = pmt - juros
                if i == parcelas:
                    amort = saldo 
                    juros = pmt - amort 
                
            saldo -= amort
            
            if tipo_pagamento != 'DESCONTO':
                total_juros += juros
            
            mes_curva = int(dc_acumulados[i] / 30)
            mes_curva = max(0, min(mes_curva, len(self.curva_cdi)-1))
            cdi_aa = self.curva_cdi[mes_curva]
            
            if base_calc == '252 DU':
                taxa_funding = (1 + cdi_aa + self.spread_aa)**(du_periodo[i] / 252) - 1
            else:
                taxa_funding = (1 + cdi_aa + self.spread_aa)**(dc_periodo[i] / 360) - 1
                
            saldo_exposto = saldo_inicial
            custo_funding_mes = saldo_exposto * taxa_funding
            ecl_mes = saldo_exposto * (pd_diaria * dc_periodo[i]) * lgd
            
            total_funding += custo_funding_mes
            total_ecl += ecl_mes
            
            cronograma.append({
                'Parcela': i, 'Data': datas[i].strftime('%d/%m/%Y'), 'Dias Período': dc_periodo[i],
                'Saldo Inicial': saldo_inicial, 'Amortização': amort, 'Juros da Parcela': juros,
                'Prestação (Cliente)': amort + juros, 'Custo Funding': custo_funding_mes,
                'Perda Esperada (ECL)': ecl_mes, 'Saldo Final': max(0.0, saldo) 
            })

        receita_total = total_juros + self.tac_fixa
        custo_opex = valor_bruto * self.opex_pct
        lucro_liquido = receita_total - total_funding - custo_opex
        llar = lucro_liquido - total_ecl
        
        capital_alocado = valor_bruto * self.pct_capital
        prazo_anualizado = dc_acumulados[-1] / 360
        raroc_aa = (llar / capital_alocado) / prazo_anualizado if (capital_alocado > 0 and prazo_anualizado > 0) else 0
        
        return {
            'Valor_Bruto': valor_bruto, 'Valor_Liquido': valor_liquido, 'IOF': iof_final,
            'Juros_Retido': juros_upfront, 'Lucro_Liquido': lucro_liquido, 'LLAR': llar,
            'ECL': total_ecl, 'RAROC_aa': raroc_aa, 'Dias_Totais': dc_acumulados[-1],
            'Cronograma': cronograma 
        }

    def buscar_taxa_minima(self, valor_input, dt_emissao, dt_1_venc, parcelas, base_calc, pd_aa, lgd, tipo_pagamento, is_financiado, is_isento_iof):
        func = lambda taxa: self.calcular_dre_operacao(taxa[0], valor_input, dt_emissao, dt_1_venc, parcelas, base_calc, pd_aa, lgd, tipo_pagamento, is_financiado, is_isento_iof)['LLAR']
        try:
            return fsolve(func, 0.015)[0]
        except:
            return 0.0

# ==========================================
# INTERFACE DO STREAMLIT
# ==========================================
st.set_page_config(page_title="Pricer de Crédito B2B/B2C", layout="wide")
st.title("📊 Calculadora Dinâmica (Dias Exatos)")

with st.spinner('Puxando curva de juros ao vivo da B3...'):
    df_b3 = obter_curva_b3_limpa()
    CURVA_CDI = gerar_curva_60_meses_b3(df_b3)

# ==========================================
# GAVETA DA CURVA DE JUROS (SOMENTE TABELA)
# ==========================================
with st.expander("📈 Visualizar Curva de Juros (DI B3)"):
    if df_b3 is not None:
        st.success("✅ **Curva DI ao vivo sincronizada com sucesso da B3!**")
        st.markdown("Valores utilizados para o cálculo do custo de Funding ao longo do tempo (baseado na expectativa do mercado futuro).")
        
        df_b3_display = df_b3.copy()
        df_b3_display['Taxa a.a.'] = df_b3_display['taxa'] * 100
        df_b3_display = df_b3_display.rename(columns={'mes': 'Mês'})
        df_b3_display = df_b3_display[['Mês', 'Taxa a.a.']]
        
        # A tabela ajusta-se para ocupar o ecrã com uma formatação muito profissional
        st.dataframe(df_b3_display.style.format({'Taxa a.a.': "{:.2f}%"}), use_container_width=True, hide_index=True)
    else:
        st.warning("⚠️ **Aviso:** A API da B3 não respondeu. O sistema utilizou uma curva padrão de segurança de 10.50% a.a.")

# ==========================================
# RESTO DA INTERFACE
# ==========================================
DICT_PD = {'A (Baixo Risco)': 0.005, 'B (Risco Médio)': 0.018, 'C (Risco Alto)': 0.042, 'D (Risco Crítico)': 0.084}

st.sidebar.header("🛠️ Produto e Datas Exatas")
preset = st.sidebar.selectbox("Escolha um Produto Base", ["BNPL Digital", "Antecipação Duplicatas", "Nota Comercial", "Customizado"])

is_isento_iof = False
if preset == "BNPL Digital":
    default_fluxo = 0 # PRICE
    default_lgd = 85
    default_parcelas = 12
    default_financiado = True 
elif preset == "Antecipação Duplicatas":
    default_fluxo = 2 # DESCONTO
    default_lgd = 40
    default_parcelas = 1
    default_financiado = False 
elif preset == "Nota Comercial":
    default_fluxo = 3 # BULLET
    default_lgd = 65
    default_parcelas = 1
    default_financiado = False
    is_isento_iof = True 
else:
    default_fluxo = 0
    default_lgd = 65
    default_parcelas = 12
    default_financiado = False

if is_isento_iof:
    st.sidebar.info("ℹ️ Isenção de IOF Ativada.")

tipo_pagamento = st.sidebar.selectbox("Fluxo de Caixa", ['PRICE', 'SAC', 'DESCONTO', 'BULLET', 'AMERICANO'], index=default_fluxo)

col_d1, col_d2 = st.sidebar.columns(2)
dt_emissao = col_d1.date_input("Data de Emissão", datetime.today())
dt_vencimento = col_d2.date_input("1º Vencimento", datetime.today() + relativedelta(months=1))

parcelas = st.sidebar.number_input("Qtd. Parcelas", min_value=1, max_value=360, value=default_parcelas)

dt_venc_final = pd.to_datetime(dt_vencimento) + relativedelta(months=int(parcelas) - 1)
st.sidebar.success(f"📅 **Vencimento Final:** {dt_venc_final.strftime('%d/%m/%Y')}")

base_calculo = st.sidebar.selectbox("Convenção de Taxa", ["360 DC (Dias Corridos)", "252 DU (Dias Úteis)"])
lgd = st.sidebar.slider("LGD (Loss Given Default) %", min_value=10, max_value=100, value=default_lgd) / 100

st.sidebar.markdown("---")
st.sidebar.header("⚙️ Premissas do Modelo (Avançado)")
col_p1, col_p2 = st.sidebar.columns(2)
pct_capital = col_p1.number_input("Capital Alocado (%)", min_value=0.0, value=10.5, step=0.5) / 100
opex_pct = col_p2.number_input("OpEx (% do Valor)", min_value=0.0, value=1.0, step=0.1) / 100
spread_cap = st.sidebar.number_input("Spread de Funding (a.a. %)", min_value=0.0, value=4.0, step=0.5) / 100

st.sidebar.markdown("---")
st.sidebar.header("💵 Operação e Custos")
iof_financiado = st.sidebar.toggle("Financiar Custos? (Embutir impostos/tarifas)", value=default_financiado)
valor_input = st.sidebar.number_input("Valor Principal Desejado (R$):", min_value=1000, value=100000, step=5000)

rating_selecionado = st.sidebar.selectbox("Rating do Cliente (Sugestão de PD)", list(DICT_PD.keys()))
pd_aa = st.sidebar.number_input("PD Anualizada (%)", min_value=0.0, max_value=100.0, value=DICT_PD[rating_selecionado]*100, step=0.1) / 100
taxa_testada = st.sidebar.number_input("Taxa Cobrada (% a.m.)", min_value=0.0, value=3.5, step=0.1) / 100
tac = st.sidebar.number_input("TAC Cobrada (R$)", min_value=0.0, value=500.0, step=50.0)

# Inicialização do Motor
motor = MotorPricerAnalitico(CURVA_CDI, spread_cap, pct_capital, opex_pct, tac)

# --- EXECUÇÃO ---
dre = motor.calcular_dre_operacao(taxa_testada, valor_input, dt_emissao, dt_vencimento, parcelas, base_calculo, pd_aa, lgd, tipo_pagamento, iof_financiado, is_isento_iof)
taxa_minima = motor.buscar_taxa_minima(valor_input, dt_emissao, dt_vencimento, parcelas, base_calculo, pd_aa, lgd, tipo_pagamento, iof_financiado, is_isento_iof)

# --- EXIBIÇÃO NO DASHBOARD ---
st.subheader("Resultados da Simulação")
st.caption(f"Prazo Total Projetado: **{dre['Dias_Totais']} Dias Corridos** a partir de {dt_emissao.strftime('%d/%m/%Y')}")

st.markdown("#### 1. Fluxo de Liberação (D0)")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Valor Bruto (EAD)", f"R$ {dre['Valor_Bruto']:,.2f}")
c2.metric("IOF Total", f"R$ {dre['IOF']:,.2f}")
c3.metric("TAC Fixa", f"R$ {tac:,.2f}")
c4.metric("Juros Retidos (D0)", f"R$ {dre['Juros_Retido']:,.2f}" if tipo_pagamento == 'DESCONTO' else "R$ 0.00")
c5.metric("Valor Líquido Conta", f"R$ {dre['Valor_Liquido']:,.2f}")

st.markdown("---")
st.markdown("#### 2. Indicadores de Retorno e Risco")
c_m1, c_m2, c_m3, c_m4 = st.columns(4)

c_m1.metric("Break-even (Mínima)", f"{taxa_minima*100:.2f}% a.m.", "Taxa onde LLAR = 0", delta_color="off")
c_m2.metric("Lucro Líquido", f"R$ {dre['Lucro_Liquido']:,.2f}", "Margem Financeira Pura", delta_color="normal")
c_m3.metric("Lucro Aj. Risco (LLAR)", f"R$ {dre['LLAR']:,.2f}", f"Absorveu R$ {dre['ECL']:,.2f} de Inadimplência", delta_color="normal")
c_m4.metric("RAROC Projetado (a.a.)", f"{dre['RAROC_aa']*100:.1f}%", "Retorno sobre Capital", delta_color="normal")

st.markdown("<br>", unsafe_allow_html=True)
if dre['LLAR'] > 0:
    st.success(f"✅ Operação Viável! A taxa de {(taxa_testada*100):.2f}% a.m. gera lucro após absorver custos e perda esperada.")
else:
    st.error(f"❌ Destruição de Valor! A operação não cobre os custos e inadimplência. Suba a taxa para no mínimo {(taxa_minima*100):.2f}% a.m.")

# ==========================================
# CRONOGRAMA E EXPORTAÇÃO
# ==========================================
st.markdown("---")
st.markdown("#### 3. Cronograma Financeiro e Exportação")

# Puxa o cronograma da execução
df_cronograma = pd.DataFrame(dre['Cronograma'])

# Aplica uma formatação de estilo (moeda) apenas para mostrar na tela
formato_moeda = {col: "R$ {:,.2f}" for col in df_cronograma.columns if '(R$)' in col or col in ['Saldo Inicial', 'Amortização', 'Juros da Parcela', 'Prestação (Cliente)', 'Custo Funding', 'Perda Esperada (ECL)', 'Saldo Final']}

with st.expander("Visualizar Cronograma Completo", expanded=False):
    st.dataframe(
        df_cronograma.style.format(formato_moeda),
        use_container_width=True,
        hide_index=True
    )

# Cria o CSV para download compatível com o Excel (separador ; e vírgula nos decimais)
csv = df_cronograma.to_csv(index=False, sep=';', decimal=',').encode('utf-8-sig')

st.download_button(
    label="📥 Baixar Tabela em Excel/CSV",
    data=csv,
    file_name=f"Cronograma_{preset.replace(' ', '_')}.csv",
    mime="text/csv",
)