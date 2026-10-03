package upload

import (
	"context"
	"time"

	"tallyconnector/internal/extract"
)

// StartRequest opens an incremental sync (see backend app/books.py).
type StartRequest struct {
	Company          any             `json:"company"`
	Period           extract.Period  `json:"period"`
	WantFull         bool            `json:"want_full,omitempty"`
	TallyAlterID     int64           `json:"tally_alter_id,omitempty"`
	MonitoringOptIn  bool            `json:"monitoring_opt_in,omitempty"`
	Consent          extract.Consent `json:"consent"`
	Machine          extract.Machine `json:"machine"`
	ConnectorVersion string          `json:"connector_version"`
}

// Started is the plan plus, for a sync opened with a one-time code, the token
// every later request uses.
type Started struct {
	extract.Plan
	Token      string `json:"token"`
	Monitoring bool   `json:"monitoring"`
	BankName   string `json:"bank_name"`
}

// StartWithCode opens the first sync for a company; the code is used up.
func (b *Backend) StartWithCode(ctx context.Context, code string, req StartRequest) (*Started, error) {
	var s Started
	return &s, b.upload(ctx, "/api/connector/sync/start", map[string]string{"X-Link-Code": code}, req, &s)
}

// Start opens (or resumes) a sync with a token from an earlier start.
func (b *Backend) Start(ctx context.Context, token string, req StartRequest) (*Started, error) {
	var s Started
	if err := b.upload(ctx, "/api/connector/sync/start", bearer(token), req, &s); err != nil {
		return nil, err
	}
	s.Token = token
	return &s, nil
}

// Session sends one sync's pieces; it implements extract.Sink.
type Session struct {
	B      *Backend
	Token  string
	SyncID string
}

type affected struct {
	Affected []string `json:"affected"`
}

func (s *Session) post(ctx context.Context, part string, payload any) ([]string, error) {
	var out affected
	err := s.B.upload(ctx, "/api/connector/sync/"+s.SyncID+"/"+part, bearer(s.Token), payload, &out)
	return out.Affected, err
}

func (s *Session) Masters(ctx context.Context, payload map[string]any) ([]string, error) {
	return s.post(ctx, "masters", payload)
}

func (s *Session) Vouchers(ctx context.Context, from, to time.Time, month string, vs []extract.Voucher) ([]string, error) {
	if vs == nil {
		vs = []extract.Voucher{}
	}
	return s.post(ctx, "vouchers", map[string]any{"from": from.Format("2006-01-02"), "to": to.Format("2006-01-02"), "month": month, "vouchers": vs})
}

func (s *Session) Present(ctx context.Context, from, to time.Time, guids []string) ([]string, error) {
	return s.post(ctx, "present", map[string]any{"from": from.Format("2006-01-02"), "to": to.Format("2006-01-02"), "guids": guids})
}

func (s *Session) Finish(ctx context.Context) error {
	_, err := s.post(ctx, "finish", map[string]any{})
	return err
}
