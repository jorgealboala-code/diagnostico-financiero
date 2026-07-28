#!/usr/bin/env python3
"""
Construye empresas.json para la Hoja de Diagnostico Financiero.

Este archivo no se ejecuta a mano. Lo corre GitHub Actions cuando se oprime
"Run workflow". Se conserva en el repositorio para que el origen de cada cifra
sea auditable.

Variables de entorno que recibe del workflow:
  SEC_CONTACTO  nombre y correo. La SEC lo exige en el encabezado User-Agent.
  TICKERS       lista separada por comas, por ejemplo "KO,PEP,WMT".
  ANIO          ejercicio mas reciente que se desea cargar.
  ANIOS         cuantos ejercicios de historia bajar. Por omision, 6.

Criterios de diseno
-------------------
1. Ancla fiscal real. No se busca por ano calendario. Primero se localiza la
   fecha de cierre de ejercicio de cada emisora leyendo sus formas 10-K, y a
   partir de esa fecha se extrae todo lo demas. Asi funciona igual para una
   emisora que cierra en diciembre que para una que cierra en enero o en mayo.

2. Cadena de respaldo por rubro. Cada concepto tiene varias etiquetas us-gaap
   posibles, en orden de preferencia. Se toma la primera que exista.

3. Derivacion contable. Si ninguna etiqueta existe, el rubro se deriva de
   identidades contables. La API de la SEC solo agrega hechos etiquetados con
   taxonomias estandar (us-gaap, ifrs-full, dei, srt); las emisoras que usan
   etiquetas propias quedan invisibles para la API, y la derivacion es el unico
   camino disponible.

4. Trazabilidad. Cada cifra se marca como "reportado" o "derivado". La
   herramienta muestra esa distincion al usuario.

Fuente: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
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
TICKERS = [t.strip().upper() for t in
           os.environ.get("TICKERS", "KO,PEP,WMT,NKE,CAT").split(",") if t.strip()]
ANIO_OBJETIVO = int(os.environ.get("ANIO", "2024"))
ANIOS_DATOS = int(os.environ.get("ANIOS", "6"))
SALIDA = "empresas.json"

# ---------------------------------------------------------------------------
# Mapeo de conceptos. Cada rubro lista sus etiquetas us-gaap posibles en orden
# de preferencia. La primera que exista con dato es la que se usa.
# ---------------------------------------------------------------------------

DURACION = {
    "ventas": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
        "RevenuesNetOfInterestExpense",
    ],
    "costo": [
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfSales",
        "CostOfGoodsSold",
        "CostOfServices",
        "CostOfGoodsAndServicesSoldExcludingDepreciationDepletionAndAmortization",
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
        "CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization",
        "CostOfRevenueExcludingDepreciationDepletionAndAmortization",
    ],
    "gav": [
        "SellingGeneralAndAdministrativeExpense",
        "GeneralAndAdministrativeExpense",
        "SellingGeneralAndAdministrativeExpenseExcludingDepreciationAndAmortization",
        "OtherSellingGeneralAndAdministrativeExpense",
        "SellingAndMarketingExpense",
    ],
    "depr": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "DepreciationDepletionAndAmortizationNonproduction",
        "Depreciation",
    ],
    "ebit": [
        "OperatingIncomeLoss",
    ],
    "ebt": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
    ],
    "neta": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "cfo": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "impuestos": [
        "IncomeTaxExpenseBenefit",
        "CurrentIncomeTaxExpenseBenefit",
    ],
}

INSTANTE = {
    "circulante": [
        "AssetsCurrent",
    ],
    "cxc": [
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
        "AccountsAndOtherReceivablesNetCurrent",
        "AccountsNotesAndLoansReceivableNetCurrent",
        "NotesAndLoansReceivableNetCurrent",
        "AccountsReceivableGrossCurrent",
    ],
    "ppe": [
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
        "PropertyPlantAndEquipmentOtherNet",
    ],
    "valores": [
        "MarketableSecuritiesCurrent",
        "ShortTermInvestments",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "OtherShortTermInvestments",
    ],
    "activo": [
        "Assets",
    ],
    "pasCirc": [
        "LiabilitiesCurrent",
    ],
    "deudaLp": [
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtAndFinanceLeaseNoncurrent",
        "LongTermNotesPayable",
        "LongTermDebt",
    ],
    "pasivo": [
        "Liabilities",
    ],
    "retenidas": [
        "RetainedEarningsAccumulatedDeficit",
    ],
    "capital": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
}

# Las acciones en circulacion aparecen en la portada del 10-K, con fecha
# posterior al cierre del ejercicio. Por eso se buscan tambien en la taxonomia
# dei y con una holgura amplia de dias.
ACCIONES_PORTADA = ["EntityCommonStockSharesOutstanding"]
ACCIONES = [
    "CommonStockSharesOutstanding",
    "CommonStockSharesIssued",
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
]

# El precio por accion no se reporta a la SEC. Las inversiones negociables son
# opcionales en el M-Score. Ninguno de los dos cuenta como faltante.
NO_SON_FALTANTE = {"precio", "valores", "impuestos"}

CAMPOS = list(DURACION.keys()) + list(INSTANTE.keys()) + ["acciones", "precio"]


# ---------------------------------------------------------------------------
# Acceso a la API
# ---------------------------------------------------------------------------

def bajar(url):
    peticion = urllib.request.Request(
        url, headers={"User-Agent": CONTACTO, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(peticion, timeout=45) as r:
        crudo = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            crudo = gzip.decompress(crudo)
    return json.loads(crudo.decode("utf-8"))


def dia(texto):
    return date(int(texto[0:4]), int(texto[5:7]), int(texto[8:10]))


def registros(hechos, etiquetas, unidad="USD", taxonomia="us-gaap"):
    """Reune los registros de las etiquetas dadas, conservando su prioridad."""
    salida = []
    for prioridad, etiqueta in enumerate(etiquetas):
        nodo = hechos.get(taxonomia, {}).get(etiqueta)
        if not nodo:
            continue
        for unidad_reportada, lista in nodo.get("units", {}).items():
            if unidad_reportada != unidad:
                continue
            for reg in lista:
                copia = dict(reg)
                copia["_prioridad"] = prioridad
                salida.append(copia)
    return salida


def cierres_fiscales(hechos):
    """Fechas de cierre anual segun las formas 10-K, de la mas reciente a la mas antigua."""
    fechas = set()
    for reg in registros(hechos, INSTANTE["activo"]):
        if not str(reg.get("form", "")).startswith("10-K"):
            continue
        if reg.get("fp") != "FY":
            continue
        fechas.add(reg["end"])
    return sorted(fechas, reverse=True)


def valor_duracion(hechos, etiquetas, fin):
    """Cifra de un periodo anual que termina en la fecha indicada."""
    candidatos = []
    for reg in registros(hechos, etiquetas):
        if "start" not in reg:
            continue
        try:
            inicio, termino = dia(reg["start"]), dia(reg["end"])
        except (ValueError, KeyError):
            continue
        largo = (termino - inicio).days
        if not (330 <= largo <= 400):
            continue
        if abs((termino - dia(fin)).days) > 7:
            continue
        candidatos.append((
            reg["_prioridad"],
            0 if str(reg.get("form", "")).startswith("10-K") else 1,
            reg["val"],
        ))
    if not candidatos:
        return None
    candidatos.sort()
    return candidatos[0][2]


def valor_instante(hechos, etiquetas, fecha, unidad="USD", holgura=7, taxonomia="us-gaap"):
    """Cifra de balance a la fecha indicada."""
    candidatos = []
    for reg in registros(hechos, etiquetas, unidad, taxonomia):
        if "start" in reg:
            continue
        try:
            if abs((dia(reg["end"]) - dia(fecha)).days) > holgura:
                continue
        except (ValueError, KeyError):
            continue
        candidatos.append((
            reg["_prioridad"],
            0 if str(reg.get("form", "")).startswith("10-K") else 1,
            reg["val"],
        ))
    if not candidatos:
        return None
    candidatos.sort()
    return candidatos[0][2]


# ---------------------------------------------------------------------------
# Extraccion y derivacion
# ---------------------------------------------------------------------------

def derivar(datos, origen):
    """
    Completa rubros ausentes mediante identidades contables.
    Se aplica en varias pasadas porque una derivacion puede habilitar otra.
    """

    def hay(*claves):
        return all(datos.get(k) is not None for k in claves)

    def poner(clave, valor, nota):
        if datos.get(clave) is None and valor is not None:
            datos[clave] = valor
            origen[clave] = nota

    for _ in range(3):
        # Identidad fundamental del balance. Exacta.
        if hay("activo", "capital"):
            poner("pasivo", datos["activo"] - datos["capital"], "derivado")
        if hay("activo", "pasivo"):
            poner("capital", datos["activo"] - datos["pasivo"], "derivado")
        if hay("pasivo", "capital"):
            poner("activo", datos["pasivo"] + datos["capital"], "derivado")

        # Pasivo no circulante como aproximacion de la deuda de largo plazo.
        # Incluye impuestos diferidos, pensiones y arrendamientos, de modo que
        # sobreestima el nivel. La variacion ano contra ano, que es lo unico que
        # usa el F-Score, se conserva.
        if hay("pasivo", "pasCirc"):
            poner("deudaLp", datos["pasivo"] - datos["pasCirc"], "aproximado")

        # Estructura del estado de resultados.
        if hay("ventas", "costo", "gav"):
            poner("ebit", datos["ventas"] - datos["costo"] - datos["gav"], "derivado")
        if hay("ventas", "gav", "ebit"):
            poner("costo", datos["ventas"] - datos["gav"] - datos["ebit"], "derivado")
        if hay("ventas", "costo", "ebit"):
            poner("gav", datos["ventas"] - datos["costo"] - datos["ebit"], "derivado")

        # Utilidad antes de impuestos a partir de la neta y la provision fiscal.
        if hay("neta", "impuestos"):
            poner("ebt", datos["neta"] + datos["impuestos"], "derivado")
        if hay("ebt") and datos.get("ebit") is None:
            # Sin gasto financiero explicito, se toma la utilidad antes de
            # impuestos como aproximacion de la operativa.
            poner("ebit", datos["ebt"], "aproximado")

    return datos, origen


def extraer(hechos, cierre):
    datos, origen = {}, {}

    for campo, etiquetas in DURACION.items():
        valor = valor_duracion(hechos, etiquetas, cierre)
        datos[campo] = valor
        origen[campo] = "reportado" if valor is not None else None

    for campo, etiquetas in INSTANTE.items():
        valor = valor_instante(hechos, etiquetas, cierre)
        datos[campo] = valor
        origen[campo] = "reportado" if valor is not None else None

    acciones = (
        valor_instante(hechos, ACCIONES, cierre, "shares")
        or valor_instante(hechos, ACCIONES_PORTADA, cierre, "shares", 150, "dei")
        or valor_instante(hechos, ACCIONES, cierre, "shares", 150)
    )
    datos["acciones"] = acciones
    origen["acciones"] = "reportado" if acciones is not None else None

    datos["precio"] = None
    origen["precio"] = None

    return derivar(datos, origen)


def anio_fiscal(cierre):
    """Ejercicio al que corresponde un cierre. Un cierre de enero pertenece al ano anterior."""
    d = dia(cierre)
    return d.year if d.month >= 6 else d.year - 1


# ---------------------------------------------------------------------------

def main():
    if not CONTACTO or "@" not in CONTACTO:
        print("ERROR: falta el secret SEC_CONTACTO con nombre y correo.")
        print("La SEC responde 403 a las peticiones sin contacto identificable.")
        sys.exit(1)

    print("Ejercicio mas reciente solicitado: %d" % ANIO_OBJETIVO)
    print("Ejercicios de historia por emisora: %d\n" % ANIOS_DATOS)
    print("Consultando el directorio de emisoras de la SEC...\n")
    directorio = bajar("https://www.sec.gov/files/company_tickers.json")
    ciks = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in directorio.values()}

    salida = {}
    for ticker in TICKERS:
        cik = ciks.get(ticker)
        if not cik:
            print("  %-6s no aparece en el directorio de la SEC" % ticker)
            continue

        try:
            time.sleep(0.2)   # el limite de la SEC es de 10 peticiones por segundo
            paquete = bajar("https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json" % cik)
        except urllib.error.HTTPError as e:
            print("  %-6s error HTTP %s" % (ticker, e.code))
            continue
        except Exception as e:
            print("  %-6s fallo la descarga: %s" % (ticker, e))
            continue

        hechos = paquete.get("facts", {})
        cierres = cierres_fiscales(hechos)
        if len(cierres) < 2:
            print("  %-6s sin dos cierres anuales en EDGAR" % ticker)
            continue

        # Punto de partida: el cierre mas reciente cuyo ejercicio no rebase el solicitado.
        arranque = 0
        for i, c in enumerate(cierres):
            if anio_fiscal(c) <= ANIO_OBJETIVO:
                arranque = i
                break

        seleccion = cierres[arranque:arranque + ANIOS_DATOS]
        if len(seleccion) < 2:
            print("  %-6s sin ejercicio comparativo disponible" % ticker)
            continue

        anios = []
        for cierre in seleccion:
            datos, origen = extraer(hechos, cierre)
            anios.append({
                "fiscal": anio_fiscal(cierre),
                "cierre": cierre,
                "datos": datos,
                "origen": origen,
            })

        reciente = anios[0]
        faltantes = [k for k in CAMPOS
                     if reciente["datos"].get(k) is None and k not in NO_SON_FALTANTE]
        derivados = [k for k, v in reciente["origen"].items()
                     if v in ("derivado", "aproximado")]

        estado = "completo" if not faltantes else "faltan: " + ", ".join(faltantes)
        if derivados:
            estado += "  [derivados: %s]" % ", ".join(sorted(derivados))

        print("  %-6s %-30s %d ejercicios  cierre %s  %s" % (
            ticker, paquete.get("entityName", "")[:30], len(anios),
            reciente["cierre"], estado))

        salida[ticker] = {
            "ticker": ticker,
            "nombre": paquete.get("entityName", ticker),
            "cik": cik,
            "anios": anios,
        }

    if not salida:
        print("\nERROR: no se pudo cargar ninguna emisora.")
        sys.exit(1)

    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, separators=(",", ":"))

    total = sum(len(e["anios"]) for e in salida.values())
    print("\nSe escribio %s con %d emisoras y %d ejercicios." % (SALIDA, len(salida), total))
    print("El precio por accion se captura manualmente en la herramienta.")


if __name__ == "__main__":
    main()
