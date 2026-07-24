#!/usr/bin/env python3
"""
Baja los datos financieros de EDGAR y escribe empresas.json.

No se ejecuta a mano. Lo corre GitHub cuando aprietas "Run workflow".

Se configura con tres variables de entorno que el workflow le pasa:
  SEC_CONTACTO  - nombre y correo, requerido por la SEC
  TICKERS       - lista separada por comas, por ejemplo "KO,PEP,WMT"
  ANIO          - ejercicio mas reciente a cargar, por ejemplo "2024"

Como resuelve el ano fiscal:
  No busca por ano calendario. Primero localiza el cierre fiscal real de cada
  emisora usando sus reportes 10-K, y a partir de esa fecha jala todo lo demas.
  Asi funciona igual para Coca-Cola, que cierra en diciembre, que para Walmart,
  que cierra en enero, o Nike, que cierra en mayo.

Fuente: API publica de EDGAR, sin llave y sin costo.
https://www.sec.gov/search-filings/edgar-application-programming-interfaces
"""

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date

CONTACTO = os.environ.get("SEC_CONTACTO", "").strip()
TICKERS = [t.strip().upper() for t in os.environ.get("TICKERS", "KO,PEP,WMT,NKE,CAT").split(",") if t.strip()]
ANIO_T = int(os.environ.get("ANIO", "2024"))
SALIDA = "empresas.json"

# Rubros que se reportan por periodo (estado de resultados y flujo).
DURACION = {
    "ventas": ["RevenueFromContractWithCustomerExcludingAssessedTax",
               "RevenueFromContractWithCustomerIncludingAssessedTax",
               "Revenues", "SalesRevenueNet", "RevenuesNetOfInterestExpense"],
    "costo": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfSales",
              "CostOfGoodsSold",
              "CostOfGoodsAndServicesSoldExcludingDepreciationDepletionAndAmortization",
              "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"],
    "gav": ["SellingGeneralAndAdministrativeExpense",
            "GeneralAndAdministrativeExpense",
            "SellingAndMarketingExpense",
            "OtherSellingGeneralAndAdministrativeExpense"],
    "depr": ["DepreciationDepletionAndAmortization",
             "DepreciationAmortizationAndAccretionNet",
             "DepreciationAndAmortization", "Depreciation"],
    "ebit": ["OperatingIncomeLoss",
             "IncomeLossFromContinuingOperationsBeforeInterestExpenseInterestIncomeIncomeTaxesExtraordinaryItemsNoncontrollingInterestsNet"],
    "ebt": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"],
    "neta": ["NetIncomeLoss", "ProfitLoss",
             "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
}

# Rubros de balance, que se reportan a una fecha.
INSTANTE = {
    "circulante": ["AssetsCurrent"],
    "cxc": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent",
            "AccountsAndOtherReceivablesNetCurrent",
            "AccountsNotesAndLoansReceivableNetCurrent",
            "AccountsReceivableGrossCurrent"],
    "ppe": ["PropertyPlantAndEquipmentNet",
            "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"],
    "valores": ["MarketableSecuritiesCurrent", "ShortTermInvestments",
                "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "activo": ["Assets"],
    "pasCirc": ["LiabilitiesCurrent"],
    "deudaLp": ["LongTermDebtNoncurrent", "LongTermDebt",
                "LongTermDebtAndCapitalLeaseObligations",
                "LongTermNotesPayable", "LongTermDebtAndFinanceLeaseNoncurrent"],
    "pasivo": ["Liabilities"],
    "retenidas": ["RetainedEarningsAccumulatedDeficit"],
    "capital": ["StockholdersEquity",
                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}

ACCIONES_PORTADA = ["EntityCommonStockSharesOutstanding"]

ACCIONES = ["CommonStockSharesOutstanding", "CommonStockSharesIssued",
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfSharesOutstandingBasic"]

# El precio de mercado no esta en EDGAR y las inversiones negociables son
# opcionales para el Beneish. Ninguno de los dos cuenta como hueco.
NO_SON_HUECO = {"precio", "valores"}


def bajar(url):
    peticion = urllib.request.Request(
        url, headers={"User-Agent": CONTACTO, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(peticion, timeout=30) as r:
        crudo = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            crudo = gzip.decompress(crudo)
    return json.loads(crudo.decode("utf-8"))


def dia(texto):
    return date(int(texto[0:4]), int(texto[5:7]), int(texto[8:10]))


def registros(hechos, etiquetas, unidad="USD", taxonomia="us-gaap"):
    """Junta todos los registros de las etiquetas dadas, en orden de preferencia."""
    salida = []
    for prioridad, etiqueta in enumerate(etiquetas):
        nodo = hechos.get(taxonomia, {}).get(etiqueta)
        if not nodo:
            continue
        for uni, lista in nodo.get("units", {}).items():
            if uni != unidad:
                continue
            for reg in lista:
                reg = dict(reg)
                reg["_prioridad"] = prioridad
                salida.append(reg)
    return salida


def cierres_fiscales(hechos):
    """Devuelve las fechas de cierre anual reportadas en 10-K, de mas nueva a mas vieja."""
    fechas = set()
    for reg in registros(hechos, INSTANTE["activo"]):
        if not str(reg.get("form", "")).startswith("10-K"):
            continue
        if reg.get("fp") != "FY":
            continue
        fechas.add(reg["end"])
    return sorted(fechas, reverse=True)


def valor_duracion(hechos, etiquetas, inicio, fin):
    """Valor de un periodo anual que termina en `fin`. Tolera unos dias de holgura."""
    candidatos = []
    for reg in registros(hechos, etiquetas):
        if "start" not in reg:
            continue
        try:
            d_ini, d_fin = dia(reg["start"]), dia(reg["end"])
        except (ValueError, KeyError):
            continue
        largo = (d_fin - d_ini).days
        if not (330 <= largo <= 400):
            continue
        if abs((d_fin - dia(fin)).days) > 7:
            continue
        candidatos.append((reg["_prioridad"],
                           0 if str(reg.get("form", "")).startswith("10-K") else 1,
                           reg["val"]))
    if not candidatos:
        return None
    candidatos.sort()
    return candidatos[0][2]


def valor_instante(hechos, etiquetas, fecha, unidad="USD", holgura=7, taxonomia="us-gaap"):
    candidatos = []
    for reg in registros(hechos, etiquetas, unidad, taxonomia):
        if "start" in reg:
            continue
        try:
            if abs((dia(reg["end"]) - dia(fecha)).days) > holgura:
                continue
        except (ValueError, KeyError):
            continue
        candidatos.append((reg["_prioridad"],
                           0 if str(reg.get("form", "")).startswith("10-K") else 1,
                           reg["val"]))
    if not candidatos:
        return None
    candidatos.sort()
    return candidatos[0][2]


def extraer(hechos, cierre, cierre_previo):
    fila = {}
    for campo, etiquetas in DURACION.items():
        fila[campo] = valor_duracion(hechos, etiquetas, cierre_previo, cierre)
    for campo, etiquetas in INSTANTE.items():
        fila[campo] = valor_instante(hechos, etiquetas, cierre)
    # Las acciones en circulacion vienen en la portada del 10-K, con fecha
    # posterior al cierre. Por eso la holgura amplia y la busqueda en dei.
    fila["acciones"] = (
        valor_instante(hechos, ACCIONES, cierre, "shares")
        or valor_instante(hechos, ACCIONES_PORTADA, cierre, "shares", 150, "dei")
        or valor_instante(hechos, ACCIONES, cierre, "shares", 150))
    fila["precio"] = None

    # Derivaciones contables exactas, para rubros que muchas emisoras no etiquetan.
    if fila["pasivo"] is None and fila["activo"] is not None and fila["capital"] is not None:
        fila["pasivo"] = fila["activo"] - fila["capital"]
    if fila["deudaLp"] is None and fila["pasivo"] is not None and fila["pasCirc"] is not None:
        # Aproximacion: todo el pasivo que no es circulante.
        fila["deudaLp"] = fila["pasivo"] - fila["pasCirc"]
    if fila["ebit"] is None and None not in (fila["ventas"], fila["costo"], fila["gav"]):
        fila["ebit"] = fila["ventas"] - fila["costo"] - fila["gav"]
    if fila["costo"] is None and fila["ventas"] is not None and fila["ebit"] is not None \
            and fila["gav"] is not None:
        fila["costo"] = fila["ventas"] - fila["gav"] - fila["ebit"]

    return fila


def main():
    if not CONTACTO or "@" not in CONTACTO:
        print("ERROR: falta el secret SEC_CONTACTO con tu nombre y correo.")
        print("La SEC responde 403 a peticiones sin contacto identificable.")
        sys.exit(1)

    print("Ejercicio objetivo: %d" % ANIO_T)
    print("Bajando el directorio de emisoras de la SEC...\n")
    directorio = bajar("https://www.sec.gov/files/company_tickers.json")
    ciks = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in directorio.values()}

    salida = {}
    for ticker in TICKERS:
        cik = ciks.get(ticker)
        if not cik:
            print("  %-6s no existe en EDGAR, se omite" % ticker)
            continue
        try:
            time.sleep(0.2)   # el limite de la SEC son 10 peticiones por segundo
            paquete = bajar("https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json" % cik)
        except urllib.error.HTTPError as e:
            print("  %-6s error HTTP %s" % (ticker, e.code))
            continue
        except Exception as e:
            print("  %-6s fallo: %s" % (ticker, e))
            continue

        hechos = paquete.get("facts", {})
        cierres = cierres_fiscales(hechos)
        if len(cierres) < 2:
            print("  %-6s sin dos cierres anuales en EDGAR, se omite" % ticker)
            continue

        # Cierre cuyo ejercicio corresponde al ano pedido. Para emisoras con ano
        # fiscal desfasado, el cierre de un ejercicio puede caer en el ano siguiente.
        objetivo = None
        for i, c in enumerate(cierres):
            anio_fiscal = dia(c).year if dia(c).month >= 6 else dia(c).year - 1
            if anio_fiscal <= ANIO_T and i + 1 < len(cierres):
                objetivo = i
                break
        if objetivo is None:
            objetivo = 0
        if objetivo + 1 >= len(cierres):
            print("  %-6s sin comparativo disponible, se omite" % ticker)
            continue

        cierre_t = cierres[objetivo]
        cierre_p = cierres[objetivo + 1]
        cierre_pp = cierres[objetivo + 2] if objetivo + 2 < len(cierres) else None

        t = extraer(hechos, cierre_t, cierre_p)
        p = extraer(hechos, cierre_p, cierre_pp) if cierre_pp else extraer(hechos, cierre_p, cierre_p)

        huecos = [k for k, val in t.items() if val is None and k not in NO_SON_HUECO]
        nota = "completo" if not huecos else "faltan: " + ", ".join(huecos)
        print("  %-6s %-32s cierre %s  %s" % (
            ticker, paquete.get("entityName", "")[:32], cierre_t, nota))

        salida[ticker] = {
            "nombre": "%s — %s" % (ticker, paquete.get("entityName", ticker)),
            "cierre": cierre_t,
            "t": t,
            "p": p,
        }

    if not salida:
        print("\nERROR: no se pudo cargar ninguna empresa.")
        sys.exit(1)

    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=1)
    print("\nEscrito %s con %d empresas." % (SALIDA, len(salida)))
    print("El precio por accion se captura a mano en la herramienta.")


if __name__ == "__main__":
    main()
