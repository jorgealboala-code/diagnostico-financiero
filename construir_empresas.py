#!/usr/bin/env python3
"""
Baja los datos financieros de EDGAR y escribe empresas.json.

Este archivo NO se ejecuta a mano. Lo corre GitHub cada vez que aprietas
"Run workflow". Esta aqui para que el proceso quede auditable.

Se configura con tres variables de entorno que el workflow le pasa:
  SEC_CONTACTO  - nombre y correo, requerido por la SEC
  TICKERS       - lista separada por comas, por ejemplo "KO,PEP,WMT"
  ANIO          - ejercicio mas reciente a cargar, por ejemplo "2024"

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

CONTACTO = os.environ.get("SEC_CONTACTO", "").strip()
TICKERS = [t.strip().upper() for t in os.environ.get("TICKERS", "KO,PEP,WMT,NKE,CAT").split(",") if t.strip()]
ANIO_T = int(os.environ.get("ANIO", "2024"))
ANIO_PREVIO = ANIO_T - 1
SALIDA = "empresas.json"

# Etiquetas us-gaap que puede usar cada rubro, en orden de preferencia.
# Las emisoras no siempre etiquetan igual; se toma la primera que exista.
DURACION = {
    "ventas": ["RevenueFromContractWithCustomerExcludingAssessedTax",
               "RevenueFromContractWithCustomerIncludingAssessedTax",
               "Revenues", "SalesRevenueNet"],
    "costo": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gav": ["SellingGeneralAndAdministrativeExpense", "GeneralAndAdministrativeExpense"],
    "depr": ["DepreciationDepletionAndAmortization",
             "DepreciationAmortizationAndAccretionNet", "Depreciation"],
    "ebit": ["OperatingIncomeLoss"],
    "ebt": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "neta": ["NetIncomeLoss", "ProfitLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
}

INSTANTE = {
    "circulante": ["AssetsCurrent"],
    "cxc": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "ppe": ["PropertyPlantAndEquipmentNet"],
    "valores": ["MarketableSecuritiesCurrent", "ShortTermInvestments"],
    "activo": ["Assets"],
    "pasCirc": ["LiabilitiesCurrent"],
    "deudaLp": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "pasivo": ["Liabilities"],
    "retenidas": ["RetainedEarningsAccumulatedDeficit"],
    "capital": ["StockholdersEquity",
                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}

ACCIONES = ["CommonStockSharesOutstanding",
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfSharesOutstandingBasic"]


def bajar(url):
    peticion = urllib.request.Request(
        url, headers={"User-Agent": CONTACTO, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(peticion, timeout=30) as r:
        crudo = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            crudo = gzip.decompress(crudo)
    return json.loads(crudo.decode("utf-8"))


def buscar(hechos, etiquetas, marco, unidad="USD"):
    for etiqueta in etiquetas:
        nodo = hechos.get("us-gaap", {}).get(etiqueta)
        if not nodo:
            continue
        for uni, registros in nodo.get("units", {}).items():
            if uni != unidad:
                continue
            for reg in registros:
                if reg.get("frame") == marco:
                    return reg["val"]
    return None


def extraer(hechos, anio):
    fila = {}
    for campo, etiquetas in DURACION.items():
        fila[campo] = buscar(hechos, etiquetas, "CY%d" % anio)
    for campo, etiquetas in INSTANTE.items():
        fila[campo] = buscar(hechos, etiquetas, "CY%dQ4I" % anio)
    fila["acciones"] = (buscar(hechos, ACCIONES, "CY%dQ4I" % anio, "shares")
                        or buscar(hechos, ACCIONES, "CY%d" % anio, "shares"))
    fila["precio"] = None   # EDGAR no publica precio de mercado
    return fila


def main():
    if not CONTACTO or "@" not in CONTACTO:
        print("ERROR: falta el secret SEC_CONTACTO con tu nombre y correo.")
        print("La SEC responde 403 a peticiones sin contacto identificable.")
        sys.exit(1)

    print("Ejercicio %d contra %d" % (ANIO_T, ANIO_PREVIO))
    print("Bajando el directorio de emisoras de la SEC...")
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
        t = extraer(hechos, ANIO_T)
        p = extraer(hechos, ANIO_PREVIO)

        huecos = [k for k, val in t.items() if val is None and k != "precio"]
        nota = "completo" if not huecos else "faltan: " + ", ".join(huecos)
        print("  %-6s %-34s %s" % (ticker, paquete.get("entityName", "")[:34], nota))

        salida[ticker] = {
            "nombre": "%s — %s" % (ticker, paquete.get("entityName", ticker)),
            "t": t,
            "p": p,
        }

    if not salida:
        print("ERROR: no se pudo cargar ninguna empresa.")
        sys.exit(1)

    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=1)
    print("\nEscrito %s con %d empresas." % (SALIDA, len(salida)))


if __name__ == "__main__":
    main()
