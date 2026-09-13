"""Генератор потока workload'ов (ADR 0015).

Workload — frontend тестового сервиса `w-<n>`: Deployment + Service NodePort в
кластере федерации и Backend + HTTPRoute на шлюзе control-кластера (ADR 0017).
До появления планировщика (задача 6) генератор размещает сам, по кругу.
"""
