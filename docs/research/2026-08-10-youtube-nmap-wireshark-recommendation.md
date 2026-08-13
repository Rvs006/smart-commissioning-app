# YouTube recommendation: Nmap and Wireshark for Smart Commissioning

Checked 2026-08-10. The goal was one useful YouTube video under three hours, chosen for this application's actual network behaviour rather than for broad ethical-hacking coverage.

## Recommendation

Watch **[How Nmap really works // And how to catch it // Stealth scan vs TCP scan // Wireshark analysis](https://www.youtube.com/watch?v=F2PXe_o7KqM)** by David Bombal with Chris Greer. Runtime: **44:03**. Published: **2022-03-11**. The first-party [video page and chapter list](https://davidbombal.com/how-nmap-really-works-and-how-to-catch-it-stealth-scan-vs-tcp-scan-wireshark-analysis/) cover:

- TCP SYN scans versus full TCP `connect()` scans
- Wireshark IP-address and TCP-port filters
- how each Nmap scan appears in a packet capture
- TCP conversation completeness
- finding relevant traffic in a large capture

This is the strongest fit because the application's IP engine is an authorized, throttled **TCP-connect** sweep over CIDRs, ranges, or registered addresses. It uses TCP ports 80, 443, 1883, and 502 by default, while BACnet/IP on UDP 47808 is handled separately by Who-Is discovery. Those behaviours are documented directly in [`ip_scan.py`](../../core/smart_commissioning_core/engines/ip_scan.py). The selected video spends its central section on the exact distinction between SYN and `connect()` scans, then inspects both in Wireshark.

The technical explanation also agrees with the primary documentation. Nmap defines `-sT` as an operating-system `connect` call and contrasts it with the half-open `-sS` scan; its UDP scan is a separate `-sU` mode ([Nmap port-scanning techniques](https://nmap.org/book/man-port-scanning-techniques.html)). Nmap accepts CIDR targets such as `192.168.10.0/24` ([Nmap target specification](https://nmap.org/book/man-target-specification.html)). Wireshark supports filters by IP address, CIDR subnet, and TCP-port sets ([Wireshark display-filter guide](https://www.wireshark.org/docs/wsug_html_chunked/ChWorkBuildDisplayFilterSection.html)), while its TCP guide explains the `tcp.completeness` field used in the lesson ([Wireshark TCP analysis](https://www.wireshark.org/docs/wsug_html_chunked/ChAdvTCPAnalysis.html)).

The presenters are credible for this topic. David Bombal identifies himself as CCIE #11023 Emeritus with more than 15 years of network-training experience ([first-party biography](https://courses.davidbombal.com/p/about)). The Wireshark Foundation's own learning page features Chris Greer for Wireshark training ([Wireshark Learn](https://www.wireshark.org/learn.html)).

## Candidates compared

| Candidate | Verified runtime | Published | Coverage and decision |
| --- | ---: | --- | --- |
| [David Bombal with Chris Greer: How Nmap really works](https://www.youtube.com/watch?v=F2PXe_o7KqM) | **44:03** | 2022-03-11 | **Winner.** Integrates Nmap scan mechanics with Wireshark packet analysis and maps directly to the app's TCP-connect scanner. Includes a downloadable PCAP in the first-party chapter page. |
| [Hacker Joe: NMAP Full Guide](https://www.youtube.com/watch?v=JHAMj2vN2oU) | **1:23:58** | 2024-02-10 | Broader Nmap syllabus: installation, basic and advanced scans, ports, service/OS detection, timing, NSE, output, and Zenmap. It offers more command coverage but does not teach Wireshark alongside the scans. |
| [Hacker Joe: Mastering Wireshark](https://www.youtube.com/watch?v=a_4MjV_-7Sw) | **54:30** | 2024-04-06 | Covers capture, filters, profiles, statistics, TCP, UDP, DHCP, and DNS. Useful for packet-analysis basics, but Nmap is absent and the material is less tightly matched to the app's scan implementation. |
| [Ermin Kreponic: The Complete Wireshark Course preview](https://www.youtube.com/watch?v=vUdOxcRJgME) | **57:17** | 2016-09-24 | Introduces networking terms, Wireshark setup and CLI, then Wireshark with Nmap at 40:10. It is an older one-hour preview of a longer paid course, so the combined topic gets less practical depth than the winner. |

Durations and publication dates above were checked against each YouTube watch page's metadata on 2026-08-10. Topic lists come from the creators' chapter descriptions on those same pages.

## What the winner will not cover

The 44-minute lesson gives the right mental model for this app's TCP scan, but three field topics remain:

1. **CIDR and routing fundamentals.** Learn subnet masks, default routes, source adapters, VLAN boundaries, and why a broadcast stays within its broadcast domain.
2. **BACnet/IP.** Study UDP 47808, Who-Is/I-Am, BBMD foreign-device registration, and BVLC results. The app deliberately keeps this out of its TCP scanner.
3. **MQTT and TLS.** Learn CONNECT/CONNACK, SUBSCRIBE/SUBACK, PUBLISH, QoS, retained messages, keepalive, port 1883, and TLS or mTLS on 8883. Encryption lets Wireshark show the TCP/TLS session while hiding MQTT payloads unless suitable session keys are available.

My preference is to watch the winner once, then spend the remaining time on one authorized lab capture. Scan a small approved CIDR or one test host on the ports the app uses, and compare a TCP SYN attempt with a full TCP connection. Filter the capture by the target IP and port, then account for every SYN, SYN-ACK, RST, timeout, and completed handshake. That physical packet-by-packet check is more useful here than memorising fifty Nmap switches.

Run scans only against systems and address ranges you are authorized to test. The application itself follows the same rule: live discovery requires explicit authorization, and dry-run mode sends no packets ([scan safety implementation](../../core/smart_commissioning_core/engines/ip_scan.py)).
