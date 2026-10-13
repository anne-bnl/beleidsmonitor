# Beleidsmonitor Natuur en Milieu

Persoonlijk hulpmiddel van een beleidsadviseur Natuur en Milieu bij Bouwend Nederland.
Het programma controleert elke ochtend een vaste lijst openbare bronnen (parlement,
officiële bekendmakingen, rechtspraak, Europa, provincies, kennisinstellingen,
natuurorganisaties en media) op nieuwe en gewijzigde publicaties.

Er gebeurt hier geen inhoudelijke beoordeling. Dit project verzamelt alleen openbare
publicaties en legt per bron vast wat er gecontroleerd is. De selectie en de
samenvattingen worden elders gemaakt.

## Wat staat waar

| Map of bestand | Inhoud |
| --- | --- |
| `config/bronnen.yml` | De bronnen die worden gecontroleerd |
| `config/trefwoorden.yml` | Ruime trefwoordenlijst voor bronnen met grote aantallen publicaties |
| `collect.py` | Het verzamelprogramma |
| `.github/workflows/verzamelen.yml` | Het dagelijkse tijdschema (04:15 UTC) |
| `data/status.json` | Per bron: gelukt of mislukt, aantallen, toelichting |
| `data/kandidaten.json` | Wat de laatste run heeft doorgelaten |
| `data/recent.json` | Wat de afgelopen 14 dagen is doorgelaten |

## Handmatig starten

Ga naar het tabblad **Actions**, kies **Dagelijkse verzameling** en klik op **Run workflow**.
