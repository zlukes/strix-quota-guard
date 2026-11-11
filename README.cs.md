# Strix quota guard

[English](README.md) | **Čeština**

Čitelný samostatný Python skript pro **lokální headless Strix** a týdenní
ChatGPT subscription kvótu. Python 3.9+, pouze standardní knihovna, macOS/Linux
(POSIX). Není součástí Strix ani oficiální integrací OpenAI.

## Instalace

```sh
git clone https://github.com/zlukes/strix-quota-guard.git ~/strix-quota-guard
```

Nejsou potřeba žádné pip balíčky. Strix musí být nainstalovaný samostatně.
Pokud adresář již existuje, nepřepisujte jej; použijte jinou cestu nebo aktualizujte existující clone.

## Použití

Strix musí být již nainstalovaný a přihlášený přes `strix auth login chatgpt`.
V jeho konfiguraci používejte subscription model `chatgpt/<váš-model>`, nikoli
placený API model. Guard konfiguraci modelů nemění, žádný fallback na placené
API neimplementuje a neumí kontrolovat náklady jiných providerů. Vlastní
`--executable` musí být důvěryhodná kompatibilní instalace Strix používající
stejný HOME a auth soubor, nikoli launcher přepínající uživatele/profil.

```sh
python3 ~/strix-quota-guard/strix_quota_guard.py --help

# Jen jednorázové čtení kvóty, bez spuštění Strix:
python3 ~/strix-quota-guard/strix_quota_guard.py --check

# Spouštějte ze svého pracovního adresáře, kde chcete mít strix_runs/.
# Testujte pouze cíle, pro které máte oprávnění.
python3 ~/strix-quota-guard/strix_quota_guard.py -- -t https://example.invalid

# Volitelné intervaly v sekundách a konkrétní instalace Strix:
python3 ~/strix-quota-guard/strix_quota_guard.py \
  --interval 15 --timeout 10 --grace 60 --executable /cesta/ke/strix \
  -- -t https://example.invalid

# Později, ze STEJNÉHO pracovního adresáře, po uvolnění kvóty:
python3 ~/strix-quota-guard/strix_quota_guard.py -- --resume NAZEV_BEHU
```

`NAZEV_BEHU` je existující adresář v `./strix_runs/`. Guard zachovává pracovní
adresář, stav nepřesouvá ani nemaže. `--resume` je argument **Strix za `--`**, ne
samostatný režim guardu. Obnovení vždy projde novou kontrolou kvóty; při >=80 %
se znovu nespustí. Automatické čekání na reset ani automatický restart není
implementován. Samotné `--resume` bez názvu je odmítnuto (vyžadovalo by picker).

Guard vždy přidá `-n`; interaktivní režim není podporován. Standardní vstup dítěte
je `/dev/null`; výstup Strix zůstává v terminálu. Nejde o daemon: nová POSIX session
izoluje procesní skupinu pro signály, ale guard zůstává v popředí.

## Příklad: audit lokálního projektu

Následující příkaz spusťte v adresáři auditovaného projektu. Proměnné prostředí
se předají Strixu beze změny. Modely jsou příkladem konkrétní konfigurace;
jejich dostupnost závisí na vašem účtu a verzi Strix.

```sh
STRIX_LLM="chatgpt/gpt-6.1-sol" \
STRIX_REASONING_EFFORT="low" \
STRIX_DEDUPE_MODEL="chatgpt/gpt-6-luna" \
python3 ~/strix-quota-guard/strix_quota_guard.py -- \
--target ./ --scan-mode quick \
--instruction "I own this repository and authorize a security audit of its source code and isolated local test deployment. Review the code for vulnerabilities, validate only with non-destructive local tests using synthetic data, and recommend fixes. Production systems, external hosts, third-party services, real credentials, and real user data are out of scope. Report affected files and lines, evidence, impact, and remediation. Distinguish confirmed findings from hypotheses. Communicate with me in Czech and write all findings, explanations, and the final report in Czech. Preserve code, commands, file paths, and identifiers in their original form."
```

`--scan-mode quick` uvádějte pouze jednou a měňte podle potřeby.
Vše za oddělovačem `--` jsou argumenty Strixu, před ním volby hlídače.
Instrukce vymezují povolený rozsah, ale nenahrazují síťovou a datovou izolaci testovacího prostředí.

Po uvolnění týdenní kvóty pokračujte ze stejného pracovního adresáře:

```sh
STRIX_LLM="chatgpt/gpt-6.1-sol" \
STRIX_REASONING_EFFORT="low" \
STRIX_DEDUPE_MODEL="chatgpt/gpt-6-luna" \
python3 ~/strix-quota-guard/strix_quota_guard.py -- \
--resume NAZEV_BEHU
```

Při obnovení nepřidávejte `--target`; zachovejte `strix_runs/` a pracovní soubory.

## Jak funguje

1. Před spuštěním Strix načte `~/.strix/subscription-auth.json` a provede pouze
   `GET https://chatgpt.com/backend-api/wham/usage`.
2. Použije Strix formát `{"codex": {"type": "oauth", "access": "…",
   "refresh": "…", "account_id": "…", "expires_at": 1234567890}}`.
   `expires_at` jsou Unix sekundy. Posílá `Authorization: Bearer <token>` a
   `ChatGPT-Account-Id`. Refresh je pouze ověřen jako přítomný, nikam se neposílá.
3. V `rate_limit` hledá právě jedno okno s `limit_window_seconds == 604800`.
   Rozhoduje `used_percent`, ne pořadí ani název `primary_window`/`secondary_window`.
   Chybějící, nejednoznačné nebo neplatné týdenní údaje znamenají chybu, ne 0 %.
4. Pod 80 % spustí `[strix, -n, ...argumenty]` bez shellu. Výchozí kontrola je
   každých 30 s po dokončení předchozí kontroly; žádný request se neopakuje
   automaticky. Krátkodobé limity (např. 5 hodin) tento guard neřídí.
5. Při >=80 % **nebo při chybě kontroly** pošle skupině dítěte jednou `SIGINT`
   a čeká na skončení hlavního procesu. Při předběžné chybě dítě vůbec nespustí.

Auth soubor se při každém dotazu čte znovu, aby se převzaly tokeny obnovené
samotným Strix. Guard jej **nikdy nezapisuje, neprovádí refresh ani rotaci tokenů**
(zamezení závodu o jednorázový refresh token). Expirace včetně bezpečnostní rezervy
`--timeout`, HTTP 401 nebo neplatný auth soubor vedou k bezpečnému zastavení.
Po skončení běhu obnovte přihlášení přes `strix auth login chatgpt` a spusťte guard znovu.
Nikdy nevkládejte token do argumentů ani logů.

Přesměrování HTTP se odmítá, endpoint nejde přepnout volbou CLI. TLS ověření
zůstává zapnuté. Běžné proxy nastavení prostředí respektuje Python urllib.
Chybové odpovědi, tokeny, account ID ani text síťových výjimek se nelogují.
Maximální přijatá odpověď usage je 1 MiB. `--timeout` je timeout síťových operací
urllib, nikoli garantovaný celkový deadline celé transakce.

## Ukončování a omezení

- Upstream headless Strix obsluhuje SIGINT/SIGTERM/SIGHUP přes
  `report_state.cleanup(status="interrupted")`, pak ukončí proces. Guard posílá
  odpovídající první uživatelský signál dál; opakované signály už nepřerušují cleanup.
  Během rozpracovaného HTTP dotazu může být předání signálu zpožděno.
- Po `--grace` sekundách (výchozí 60) vypíše **„proces stále běží“** a dál čeká.
  **Nikdy neposílá SIGKILL a nikdy nehlásí zastavení pouze proto, že poslal signál.**
  Grace je lhůta pro varování, ne nucené zabití. Pokud dítě ignoruje signál, může
  guard čekat neomezeně; stav je nutné zkontrolovat z jiného terminálu.
- Sleduje skončení hlavního procesu, nikoli důkaz zastavení všech vzdálených úloh,
  kontejnerů či potomků, kteří opustili jeho procesní skupinu. Persistenci řídí
  Strix; při zásahu velmi brzy po startu ještě nemusí existovat obnovitelný stav.
- **80 % není tvrdá garance.** Mezi kontrolami, během probíhajících požadavků,
  zpoždění backendu a cleanupu může spotřeba narůst. Ostatní procesy na stejném
  účtu tento guard nezastaví. Pro větší rezervu zkraťte interval a sledujte účet.
- Interní endpoint a formát usage nejsou stabilní veřejné API. Autentizace byla
  ověřena čtením upstream zdrojového kódu, **nikoli živým ověřením u OpenAI**.
  Testy používají syntetické odpovědi, nikoliv důkaz dostupnosti či oprávnění
  endpointu pro konkrétní účet. Změna schématu vede k zastavení.

## Návratové kódy

| Kód | Význam |
|---|---|
| 0 | `--check`: pod limitem; jinak úspěšný návrat Strix |
| 80 | Kvóta >=80 % před startem nebo během běhu |
| 81 | Auth / usage / síť / spuštění procesu selhalo |
| 2 | Neplatné argumenty guardu |
| 130 / 143 / 129 | Uživatelský SIGINT / SIGTERM / SIGHUP předaný guardem |
| ostatní | Běžný návratový kód dítěte je zachován |

Kódy 80/81 jsou zvolené kódy guardu; pokud je vrátí samo dítě, nelze je pouze
podle exit statusu rozlišit. Guard nemění význam exit statusů Strix, například
jeho kódu 2 pro nálezy. Pokud dítě skončí signálem samo, použije se shellová
konvence `128 + číslo signálu`.

## Testy a dohledatelnost

```sh
cd ~/strix-quota-guard
python3 -m unittest -v
```

Testy nepoužívají skutečné credentials, síť ani skutečný Strix. Spouštějí neškodné
Python procesy s reálnými signal handlery: ověřují cleanup při kvótě a chybě,
uživatelské signály, čekání po grace a zamezení spuštění při selhání preflightu.
Dočasné soubory testů používají standardní dočasný adresář Pythonu.
Vlastní existující adresář lze zvolit přes `STRIX_GUARD_TEST_TMPDIR`.

Implementace vychází z upstream Strix (`strix/config/codex.py`,
`strix/interface/cli.py`, `strix/interface/cli_args.py`), commit
`685bbc8138d35fc9fe378ac37ec0d9198b9c46a3`.
