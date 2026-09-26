| прогон | режим | годен | ER отказа (k6) | ER (сервер) | p95, мс | в деградир. | degraded, с | score_loss, с | first_avoid, с |
|---|---|---|---|---|---|---|---|---|---|
| task8-s2-meta-r1 | meta | да | 2.83% (425/15000) | 2.81% | 503.2 | 0/10 | 42.0 | 12.0 | 25.3 |
| task8-s2-naive-r1 | naive | да | 16.88% (2524/14950) | 16.51% | 1002.6 | 5/9 | 40.9 | 40.9 | — |
| task8-s2-meta-r2 | meta | да | 5.20% (777/14950) | 5.13% | 550.4 | 1/10 | 39.6 | 39.6 | 68.0 |
| task8-s2-naive-r2 | naive | да | 15.00% (2250/15000) | 14.60% | 1002.9 | 5/9 | 23.0 | 23.0 | — |
| task8-s2-meta-r3 | meta | да | 9.42% (1413/15000) | 9.15% | 1002.1 | 0/10 | 20.3 | 20.3 | 26.1 |
| task8-s2-naive-r3 | naive | да | 17.13% (2569/15000) | 16.58% | 1002.9 | 5/9 | 18.3 | 18.3 | — |

- meta: ER 5.82% (мин 2.83%, макс 9.42%, n=3)
- naive: ER 16.34% (мин 15.00%, макс 17.13%, n=3)
- снижение error rate: 64.4% (цель ≥ 20%); по парам: r1: 83.2%, r2: 65.4%, r3: 45.0%
- task8-s2-meta-r1: DR в observe дошёл до DOWN: cluster-b — с failover вмешался бы
- task8-s2-naive-r1: DR в observe дошёл до DOWN: cluster-b — с failover вмешался бы
- task8-s2-naive-r1: не стали готовыми (без трафика, их ошибки вне ряда): {'cluster-b': 5}
- task8-s2-meta-r2: DR в observe дошёл до DOWN: cluster-b — с failover вмешался бы
- task8-s2-meta-r2: не стали готовыми (без трафика, их ошибки вне ряда): {'cluster-b': 1}
- task8-s2-naive-r2: DR в observe дошёл до DOWN: cluster-b — с failover вмешался бы
- task8-s2-naive-r2: не стали готовыми (без трафика, их ошибки вне ряда): {'cluster-b': 6}
- task8-s2-meta-r3: DR в observe дошёл до DOWN: cluster-b — с failover вмешался бы
- task8-s2-naive-r3: DR в observe дошёл до DOWN: cluster-b — с failover вмешался бы
- task8-s2-naive-r3: не стали готовыми (без трафика, их ошибки вне ряда): {'cluster-b': 6}
