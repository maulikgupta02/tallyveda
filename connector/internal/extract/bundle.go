// Package extract pulls a company's books out of Tally into a Bundle, the
// JSON document uploaded to the bank backend.
//
// Amount convention: every amount and balance in a Bundle is DEBIT-POSITIVE
// (debit > 0, credit < 0). Tally's XML uses the opposite sign; it is flipped
// here so the backend never has to think about it.
package extract

import "tallyconnector/internal/tally"

const SchemaVersion = 1

type Bundle struct {
	SchemaVersion    int             `json:"schema_version"`
	ConnectorVersion string          `json:"connector_version"`
	ExtractedAt      string          `json:"extracted_at"`
	Consent          Consent         `json:"consent"`
	Machine          Machine         `json:"machine"`
	Company          tally.Company   `json:"company"`
	Period           Period          `json:"period"`
	Groups           []Group         `json:"groups"`
	Ledgers          []Ledger        `json:"ledgers"`
	VoucherTypes     []Group         `json:"voucher_types"`
	StockSnapshots   []StockSnapshot `json:"stock_snapshots"`
	Vouchers         []Voucher       `json:"vouchers"`
	Warnings         []string        `json:"warnings"`
}

type Consent struct {
	AcceptedAt string `json:"accepted_at"`
	AcceptedBy string `json:"accepted_by"`
	Text       string `json:"text"`
	// MonitoringOptIn: the borrower also agreed to daily refreshes.
	MonitoringOptIn bool `json:"monitoring_opt_in"`
}

type Machine struct {
	Hostname string `json:"hostname"`
	OS       string `json:"os"`
	TallyURL string `json:"tally_url"`
	Tally    string `json:"tally_banner"`
}

type Period struct {
	From string `json:"from"`
	To   string `json:"to"`
}

// Group is used for both account groups and voucher types (name/parent tree).
type Group struct {
	Name         string `json:"name"`
	Parent       string `json:"parent"`
	ReservedName string `json:"reserved_name,omitempty"`
}

type Ledger struct {
	Name             string  `json:"name"`
	Parent           string  `json:"parent"`
	ReservedName     string  `json:"reserved_name,omitempty"`
	OpeningBalance   float64 `json:"opening_balance"`
	ClosingBalance   float64 `json:"closing_balance"`
	IsBillWise       bool    `json:"is_billwise,omitempty"`
	CreditPeriodDays *int    `json:"credit_period_days,omitempty"`
	GSTIN            string  `json:"gstin,omitempty"`
	State            string  `json:"state,omitempty"`
	Country          string  `json:"country,omitempty"`
}

type StockSnapshot struct {
	AsOf  string      `json:"as_of"`
	Items []StockItem `json:"items"`
}

type StockItem struct {
	Name         string  `json:"name"`
	Parent       string  `json:"parent"`
	Unit         string  `json:"unit,omitempty"`
	ClosingQty   float64 `json:"closing_qty"`
	ClosingValue float64 `json:"closing_value"`
}

type Voucher struct {
	GUID        string  `json:"guid"`
	MasterID    int64   `json:"master_id,omitempty"`
	AlterID     int64   `json:"alter_id,omitempty"`
	Date        string  `json:"date"`
	Type        string  `json:"type"`
	Number      string  `json:"number"`
	Party       string  `json:"party,omitempty"`
	Reference   string  `json:"reference,omitempty"`
	Narration   string  `json:"narration,omitempty"`
	IsCancelled bool    `json:"is_cancelled,omitempty"`
	IsOptional  bool    `json:"is_optional,omitempty"`
	IsInvoice   bool    `json:"is_invoice,omitempty"`
	Entries     []Entry `json:"entries"`
}

type Entry struct {
	Ledger string    `json:"ledger"`
	Amount float64   `json:"amount"`
	Bills  []BillRef `json:"bills,omitempty"`
}

type BillRef struct {
	Name   string  `json:"name"`
	Type   string  `json:"type"`
	Amount float64 `json:"amount"`
}
