package thanos

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestErrorRates(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/query" || r.URL.Query().Get("query") != ErrorRateQuery ||
			r.URL.Query().Get("partial_response") != "true" {
			t.Errorf("unexpected request %s", r.URL)
		}
		_, _ = w.Write([]byte(`{"status":"success","data":{"resultType":"vector","result":[
			{"metric":{"cluster":"cluster-a"},"value":[1700000000.1,"0.012"]},
			{"metric":{"cluster":"cluster-b"},"value":[1700000000.1,"NaN"]},
			{"metric":{},"value":[1700000000.1,"0.5"]}
		]}}`))
	}))
	defer srv.Close()
	got, err := New(srv.URL+"/", time.Second).ErrorRates(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got["cluster-a"] != 0.012 {
		t.Fatalf("got %v: NaN and unlabeled series must be dropped", got)
	}
}

func TestErrorRatesErrors(t *testing.T) {
	for name, h := range map[string]http.HandlerFunc{
		"http 503": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"status":"error","error":"no store"}`))
		},
		"not json": func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("oops")) },
		"matrix": func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(`{"status":"success","data":{"resultType":"matrix","result":[]}}`))
		},
	} {
		srv := httptest.NewServer(h)
		if _, err := New(srv.URL, time.Second).ErrorRates(context.Background()); err == nil {
			t.Errorf("%s: expected error", name)
		}
		srv.Close()
	}
}
