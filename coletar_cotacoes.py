#!/usr/bin/env python3
"""
Coletor de cotações (pontos) de ingressos Disney/Universal - Azul Viagens.

Lê o JSON interno (results.ashx) em vez de dirigir o navegador.
Requer: pip install requests

Exemplos:
  # Teste offline com o HAR exportado do Chrome
  python coletar_cotacoes.py --har tudoazul_azulviagens_com_br2.har --saida /tmp/cotacoes_teste.json

  # Online, reaproveitando uma busca já feita no navegador (um mês)
  python coletar_cotacoes.py --sessao 1782930

  # Online, 3 meses (mês atual + 2), com publicação no GitHub
  python coletar_cotacoes.py --meses 3 --publicar
"""
import argparse, calendar, datetime as dt, json, os, re, statistics, subprocess, sys, time

BASE = "https://tudoazul.azulviagens.com.br"
REPO = "/Users/brunotoigo/Claude/Projects/Agencia de Viagens"
DESTINO_ID = 1361      # Orlando (zoneID / destinationID)
LIMIAR = 0.20          # desvio máximo vs média histórica

# Identificação por CÓDIGO (estável). "kw" é só fallback por palavra-chave
# para produtos cujo código ainda não foi confirmado.
PRODUTOS = {
    "disney_all":    {"nome": "Disney 4-Park Magic Ticket", "codes": ["T0006129"], "kw": ["magic ticket"], "sazonal": True},
    "mk":            {"nome": "Magic Kingdom",       "codes": ["T0001879"]},
    "epcot":         {"nome": "EPCOT",               "codes": ["T0001881"]},
    "hs":            {"nome": "Hollywood Studios",   "codes": ["T0001883"]},
    "ak":            {"nome": "Animal Kingdom",      "codes": ["T0001885"]},
    "universal_all": {"nome": "Universal All Parks", "codes": ["4631-2-180141421003"], "kw": ["universal orlando", "all parks"], "sazonal": True},
    "eu":            {"nome": "Epic Universe",       "codes": ["4443-2-180110111031"]},
    # Desde jul/2026 IOA e USF são o mesmo produto genérico "1 Parque 1 Dia"
    "ioa":           {"nome": "Islands of Adventure",       "codes": ["4443-2-180110111007"]},
    "usf":           {"nome": "Universal Studios Florida",  "codes": ["4443-2-180110111007"]},
    "u2park":        {"nome": "Universal 2 Parques 1 Dia",  "codes": ["4443-2-180120121007"]},
}


# ----------------------------------------------------------------- coleta
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")


class Bloqueado(RuntimeError):
    pass


def nova_http():
    """Sessão HTTP com cookies, aquecida na home (como o navegador faz)."""
    import requests
    s = requests.Session()
    s.headers.update({"user-agent": UA, "accept-language": "pt-BR,pt;q=0.9"})
    try:
        s.get("https://www.azulviagens.com.br/", timeout=60)
    except Exception:
        pass  # aquecimento é opcional
    return s


def _checar(r):
    if r.status_code in (403, 429) or "Access Denied" in r.text[:500]:
        raise Bloqueado(f"Site bloqueou a requisição (HTTP {r.status_code}). "
                        "Usar coleta via navegador (Playwright).")
    r.raise_for_status()


def _get(s, url, rotulo="requisição", tentativas=3, **kw):
    """GET com repetição em caso de timeout, queda de conexão ou erro 5xx do servidor."""
    import requests
    for i in range(1, tentativas + 1):
        t0 = time.time()
        try:
            r = s.get(url, **kw)
            _checar(r)
            print(f"  {rotulo}: {time.time() - t0:.0f}s", flush=True)
            return r
        except Bloqueado:
            raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError, requests.exceptions.HTTPError) as e:
            if isinstance(e, requests.exceptions.HTTPError) and e.response is not None \
                    and e.response.status_code < 500:
                raise
            if i == tentativas:
                raise
            espera = 10 * i
            print(f"  aviso: {type(e).__name__} em '{rotulo}' após {time.time() - t0:.0f}s "
                  f"(tentativa {i}/{tentativas}); repetindo em {espera}s", flush=True)
            time.sleep(espera)


def criar_sessao(s, inicio, fim):
    """Abre a busca (Orlando, 1 adulto) por URL e lê o searchSessionID embutido no HTML."""
    r = _get(s, f"{BASE}/services/results.aspx", rotulo="criar busca",
             params={"accion": "searchservices", "startDate": inicio.isoformat(),
                     "endDate": fim.isoformat(), "destinationID": DESTINO_ID, "paxs": "10"},
             headers={"referer": "https://www.azulviagens.com.br/"}, timeout=(30, 120))
    return sessao_do_html(r.text, inicio, fim)


def sessao_do_html(html, inicio, fim):
    m = re.search(r'"searchSessionID"\s*:\s*(\d+)', html)
    if not m:
        raise RuntimeError("searchSessionID não encontrado no HTML - o site mudou?")
    info = html[m.start():m.start() + 600]
    pax = re.search(r'"paxes"\s*:\s*\{"adults"\s*:\s*(\d+)\s*,\s*"children"\s*:\s*(\d+)', info)
    if not pax or (pax.group(1), pax.group(2)) != ("1", "0"):
        raise RuntimeError(f"Ocupação inesperada na busca: {pax.groups() if pax else 'não achada'} (esperado 1 adulto)")
    ini = re.search(r'"startDate"\s*:\s*"(\d{4}-\d{2}-\d{2})', info)
    if ini and ini.group(1) != inicio.isoformat():
        raise RuntimeError(f"Data inicial da busca diferente da pedida: {ini.group(1)} != {inicio}")
    return m.group(1)


def baixar_paginas(s, sessao):
    """Baixa todas as páginas de results.ashx de uma sessão de busca."""
    def pagina(n):
        r = _get(s, f"{BASE}/services/ashx/results.ashx", rotulo=f"página {n}",
                 params={"_": "63471872", "searchSessionID": sessao,
                         "currency": "BRL", "order": "recomendadosOrden", "page": n},
                 headers={"accept": "application/json, text/javascript, */*; q=0.01",
                          "x-requested-with": "XMLHttpRequest",
                          "referer": f"{BASE}/services/results.aspx?searchSessionID={sessao}"},
                 timeout=(30, 150))
        return r.json()

    # Em sequência, como o navegador faz (em paralelo o servidor parece travar; a página 2 é pesada)
    p1 = pagina(1)
    total = p1["summary"]["pages"]
    return [p1] + [pagina(n) for n in range(2, total + 1)]


def coletar_periodo(ini, fim, dividir=True):
    """Devolve [(rótulo, páginas)]. Tenta 2x (com conexão nova); se falhar, divide o período ao meio."""
    rot = f"{ini:%d/%m}-{fim:%d/%m}"
    for tentativa in (1, 2):
        print(f"Buscando {ini} a {fim} ..." + (" (nova tentativa)" if tentativa == 2 else ""), flush=True)
        try:
            http = nova_http()
            pgs = baixar_paginas(http, criar_sessao(http, ini, fim))
            print("  ok", flush=True)
            return [(rot, pgs)]
        except Bloqueado:
            raise
        except Exception as e:
            print(f"  falhou ({type(e).__name__})", flush=True)
            if tentativa == 1:
                time.sleep(15)
    dias = (fim - ini).days
    if dividir and dias >= 7:
        meio = ini + dt.timedelta(days=dias // 2)
        print(f"  dividindo {ini} a {fim} em duas buscas menores", flush=True)
        return (coletar_periodo(ini, meio, dividir=False)
                + coletar_periodo(meio + dt.timedelta(days=1), fim, dividir=False))
    raise RuntimeError(f"não foi possível coletar {ini} a {fim}")


def paginas_do_har(caminho):
    har = json.load(open(caminho, encoding="utf-8"))
    pgs = []
    for e in har["log"]["entries"]:
        if "results.ashx" in e["request"]["url"]:
            txt = e["response"]["content"].get("text")
            if txt:
                pgs.append(json.loads(txt))
            else:
                print(f"AVISO: página sem corpo no HAR ({e['request']['url'][-8:]})", file=sys.stderr)
    return pgs


# ----------------------------------------------------------------- parse
def extrair(paginas):
    """{chave: {'por_data': {AAAA-MM-DD: pontos}, 'code': ..., 'nome_site': ...}}"""
    resultados = [r for p in paginas for r in p["results"]]
    saida, log = {}, []
    for chave, cfg in PRODUTOS.items():
        alvo = None
        for r in resultados:
            if r["service"].get("code") in cfg["codes"]:
                alvo = r
                break
        if alvo is None and cfg.get("kw"):
            for r in resultados:
                nome = (r["service"].get("name") or r["availability"].get("name") or "").lower()
                if all(k in nome for k in cfg["kw"]):
                    alvo = r
                    log.append(f"{chave}: achado por palavra-chave -> code={r['service']['code']} (fixe em PRODUTOS)")
                    break
        if alvo is None:
            if cfg.get("sazonal"):
                log.append(f"{chave}: indisponível neste período (produto sazonal) - ignorado")
            else:
                log.append(f"{chave}: ATENÇÃO - não encontrado; pode ter mudado de código no site")
            continue
        por_data = {}
        for x in alvo["availability"]["options"][0]["allPrices"]:
            pts = x["priceDetail"]["LoyaltyRules"]["redemption"]["points"]["pointsAmount"]
            if x.get("available", True) and pts and x["breakdown"]["adultPrice"] > 0:
                por_data[x["startDate"][:10]] = int(pts)
        saida[chave] = {"por_data": por_data, "code": alvo["service"]["code"]}
    return saida, log


# ----------------------------------------------------------------- validação
def referencia(data, por_data, hist):
    """Preço de referência de uma data: o da coleta anterior (mesma data); sem histórico,
    a mediana dos dias vizinhos (±3 dias) da própria coleta. Evita falso alarme por sazonalidade."""
    if hist.get(data):
        return hist[data]
    d0 = dt.date.fromisoformat(data)
    viz = [v for k, v in por_data.items()
           if k != data and abs((dt.date.fromisoformat(k) - d0).days) <= 3]
    return statistics.median(viz) if len(viz) >= 3 else None


def anomalias(novos, historico):
    out = []
    for k, d in novos.items():
        hist = historico.get(k, {}).get("por_data", {})
        for data, pts in d["por_data"].items():
            ref = referencia(data, d["por_data"], hist)
            if not ref:
                continue
            desv = (pts - ref) / ref
            if abs(desv) > LIMIAR:
                out.append((k, data, pts, round(ref), round(desv * 100, 1)))
    return out


# ----------------------------------------------------------------- main
def meses_a_coletar(n):
    hoje = dt.date.today()
    for i in range(n):
        m = hoje.month - 1 + i
        ano, mes = hoje.year + m // 12, m % 12 + 1
        ini = max(dt.date(ano, mes, 1), hoje)   # não pesquisar datas passadas
        fim = dt.date(ano, mes, calendar.monthrange(ano, mes)[1])
        yield ini, fim


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--har", help="usar um HAR local (teste offline)")
    ap.add_argument("--sessao", help="searchSessionID de uma busca já feita (1 mês)")
    ap.add_argument("--meses", type=int, default=3)
    ap.add_argument("--periodo", nargs=2, metavar=("INICIO", "FIM"), action="append",
                    help="coletar um período específico AAAA-MM-DD AAAA-MM-DD (repetível); útil para diagnóstico")
    ap.add_argument("--saida", default=os.path.join(REPO, "cotacoes.json"))
    ap.add_argument("--aceitar-anomalias", action="store_true")
    ap.add_argument("--descartar-anomalias", action="store_true")
    ap.add_argument("--publicar", action="store_true", help="git add/commit/push só do cotacoes.json")
    a = ap.parse_args()

    try:
        if a.har:
            lotes = [("HAR", paginas_do_har(a.har))]
        elif a.sessao:
            lotes = [("sessão " + a.sessao, baixar_paginas(nova_http(), a.sessao))]
        else:
            periodos = ([(dt.date.fromisoformat(i), dt.date.fromisoformat(f)) for i, f in a.periodo]
                        if a.periodo else list(meses_a_coletar(a.meses)))
            lotes = []
            for ini, fim in periodos:
                lotes += coletar_periodo(ini, fim)
                time.sleep(10)
    except Bloqueado as e:
        print(f"\nERRO: {e}\nNada foi gravado.")
        sys.exit(3)
    except Exception as e:
        print(f"\nERRO na coleta: {type(e).__name__}: {e}\nNada foi gravado; o cotacoes.json não foi alterado. "
              "Tente novamente em alguns minutos.")
        sys.exit(1)

    try:
        historico = json.load(open(a.saida, encoding="utf-8"))
    except FileNotFoundError:
        historico = {}

    novos, avisos = {}, []
    for rot, lote in lotes:
        dados, log = extrair(lote)
        avisos += [f"[{rot}] {m}" for m in log]
        for k, d in dados.items():
            novos.setdefault(k, {"por_data": {}, "code": d["code"]})["por_data"].update(d["por_data"])

    for av in avisos:
        print("•", av)

    anom = anomalias(novos, historico)
    if anom and not (a.aceitar_anomalias or a.descartar_anomalias):
        print("\n⚠️  Anomalias (>20% vs mesma data da coleta anterior, ou vs dias vizinhos se não houver histórico) - nada foi gravado:")
        for k, data, pts, ref, desv in anom:
            print(f"  {k:14} {data}  {pts:>8}  ref {ref:>8}  {desv:+.1f}%")
        print("Reexecute com --aceitar-anomalias ou --descartar-anomalias.")
        sys.exit(2)
    if anom and a.descartar_anomalias:
        for k, data, *_ in anom:
            novos[k]["por_data"].pop(data, None)

    final = {"_atualizado": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
             "_nota": "Gerado automaticamente pelo agente. Não editar manualmente.",
             "_formato_versao": 2}
    for k, d in novos.items():
        if not d["por_data"]:
            continue
        base = min(d["por_data"].values())
        final[k] = {"nome": historico.get(k, {}).get("nome", PRODUTOS[k]["nome"]),
                    "pontos_base": base, "pontos": base,
                    "por_data": dict(sorted(d["por_data"].items()))}

    with open(a.saida, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    print(f"\nGravado: {a.saida}")
    for k, d in final.items():
        if not k.startswith("_"):
            v = d["por_data"].values()
            print(f"  {k:14} {len(d['por_data']):>3} datas   min {min(v):>7}   max {max(v):>7}")

    if a.publicar:
        cwd = os.path.dirname(a.saida)
        subprocess.run(["git", "add", os.path.basename(a.saida)], cwd=cwd, check=True)
        r = subprocess.run(["git", "commit", "-m", f"Atualiza cotações de ingressos ({dt.date.today()})"],
                           cwd=cwd, capture_output=True, text=True)
        if "nothing to commit" in (r.stdout + r.stderr):
            print("Sem mudanças para publicar.")
        else:
            subprocess.run(["git", "push"], cwd=cwd, check=True)
            print("Publicado no GitHub.")


if __name__ == "__main__":
    main()
