# Образ собирается локально и загружается в kind (ADR 0014); registry не нужен.
FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/test-service .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/test-service /test-service
USER 65532:65532  # nonroot из distroless, числом ради hadolint DL3066
EXPOSE 8080
ENTRYPOINT ["/test-service"]
