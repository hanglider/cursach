# Образ DR Controller'а (ADR 0028); собирается локально и загружается в control через kind load.
FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/dr-controller .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/dr-controller /dr-controller
USER 65532:65532  # nonroot из distroless, числом ради hadolint DL3066
EXPOSE 8080 8081
ENTRYPOINT ["/dr-controller"]
