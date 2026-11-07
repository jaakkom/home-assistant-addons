# Autoterm Flow 5

Lisäosa korvaa lämmittimen ohjauspaneelin. Yksi 5 V USB-UART-sovitin kytketään paneelin johtimen tilalle. Lisäosa on väylän ainoa osapuoli: se kysyy tilan, lähettää huonelämpötilan ja päättää käynnistyksen sekä pysäytyksen.

Lämmitin on polttolaite. Ensimmäisellä kerralla valvo käynti: ensin pelkkä tila, sitten pysäytys, vasta sen jälkeen käynnistys.

## Kytkentä

Flow 5 / BINAR-5S käyttää 2400 baud, 8N1, 5 V TTL -sarjaliikennettä. 3,3 V:n sovitin ei yleensä saa vastausta.

| Sovitin | Lämmittimen johdin | Suunta |
| --- | --- | --- |
| TX | valkoinen | paneeli → lämmitin |
| RX | keltainen | lämmitin → paneeli |
| GND | sininen tai musta | yhteinen maa |

Punaista +12 V -johdinta ei kytketä sovittimeen. Se syöttää paneelia, eikä se ole sarjadataa.

Paneelia ei jätetä samaan liittimeen. Kaksi isäntää törmää, ja paneeli voi menettää yhteyden.

Mosquitto-lisäosa tarvitaan, jotta entiteetit ilmestyvät MQTT-discoveryn kautta. Lämpötilaentiteetit luetaan Home Assistantin rajapinnasta. Kirjoita ne asetukseen pilkulla erotettuna, esimerkiksi `sensor.olohuone,sensor.makuu`.

## Ohjaus

Ilmastointientiteetti pitää huoneen tavoitetta. Lämmitys ei käynnisty lisäosan käynnistyessä. Käyttäjä kytkee lämmityksen päälle ilmastointientiteetistä, kytkimestä tai valitsee esilämmityksen tai pelkän pumpun.

Huonelämpö luetaan Home Assistantin antureista. Oletuksena ohjaa kylmin lukema. Sama lukema lähetetään lämmittimelle paneelin ohjaamolämpönä. Asetusarvo ei kulje väylällä.

Lämmönpyyntö alkaa, kun lämpö on pudotuksen verran alle tavoitteen (oletus 2 °C), ja päättyy, kun se on nousun verran yli (oletus 1 °C). Vähimmäiskäynti ja vähimmäisseisonta ovat oletuksena 10 minuuttia, joten lyhyt lämpötilan pomppu ei sytytä poltinta heti uudelleen.

Nesteen yläraja on oma numero, oletus 88 °C ja alue 20–95 °C. Pudotus, jonka jälkeen poltin saa syttyä uudelleen, on oletuksena 18 °C. Raja toteutetaan lisäosan pysäytyskäskyllä. Lämmittimen omaan muistiin ei kirjoiteta asetusta, koska sille ei ole vahvistettua kehystä. Jos lämmittimen oma raja on alempi, se ehtii pysäyttää ensin.

Ilman tuoretta anturilukemaa lämmitintä ei käynnistetä. Jos lukema vanhenee ajon aikana, poltin pysäytetään. Ylikuumeneminen (koodi 1) ja lukitus (koodi 37) eivät käynnisty uudelleen, ennen kuin lämmitys kytketään pois ja takaisin päälle.

Tehotasoa ei lähetetä. Matala, keski ja korkea ovat lämmittimen omia vaiheita, ja ne näkyvät antureina.

Kaikki säädöt ovat Home Assistantin numeroita, kytkimiä tai valintoja. Muutos tallentuu tiedostoon `/data/settings.json` ja säilyy uudelleenkäynnistyksessä. Lisäosan asetussivu antaa vain ensimmäiset arvot. Jos haluat aloittaa asetussivulta uudelleen, poista tuo tiedosto.

## Polttoaine

Pumpun taajuus tulee laajennetusta 58 tavun kehyksestä. 12 V:n TH-11-pumppu (assy 4995) annostelee 0,044 ml iskua kohti. Hetkellinen kulutus on `l/h = taajuus_Hz × annos_ml × 3,6`. Kertymä integroidaan näytteiden välistä. Yli viiden sekunnin aukko ei kasvata kertymää. Kertymä on tiedostossa `/data/fuel.json`, ja sen voi nollata painikkeesta. 24 V -pumpun annos voi poiketa, joten annos on säädettävä.

Laajennettu telemetria pyydetään `PUBR0`-kättelyllä. Se on oletuksena päällä. Jos 58 tavun kehys ei tule, kättely lähetetään uudelleen noin minuutin välein.

## Vikakoodit

Vikakoodi, suomenkielinen teksti ja vikabinääri julkaistaan Home Assistantiin. Ilmoitus lähtee vain, kun koodi vaihtuu. Se poistuu, kun koodi palaa nollaan. MQTT-aihe `autoterm_flow5/event/fault` sisältää koodin ja tekstin, ja siitä voi tehdä puhelinilmoituksen automaatiolla. Sarjalinkin katkeaminen tekee oman ilmoituksen.

| Koodi | Teksti |
| --- | --- |
| 1 | Ylikuumeneminen |
| 3, 24, 26 | Huolto |
| 25 | Liian nopea lämmönnousu |
| 12 | Ylijännite |
| 14 | Jäähdytysnestepumppu tai -piiri |
| 15 | Alijännite |
| 17, 22 | Polttoainepumppu |
| 20, 30, 50 | Ei yhteyttä |
| 29, 78 | Tarkista polttoainejärjestelmä |
| 37 | Lämmitin lukittu |

Tuntematon koodi näytetään numerona.

## Testit

Laitteistoa ei tarvita yksikkötesteihin:

```bash
PYTHONPATH=autoterm_flow5 python3 -m unittest discover -s autoterm_flow5/tests
```
