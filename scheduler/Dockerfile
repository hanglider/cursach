# Образ планировщика (ADR 0027); собирается локально и загружается в control через kind load.
FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/scheduler .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/scheduler /scheduler
USER 65532:65532  # nonroot из distroless, числом ради hadolint DL3066
EXPOSE 8081
ENTRYPOINT ["/scheduler"]
