package extract

import (
	"testing"

	"tallyconnector/internal/tally"
)

func parseOne(t *testing.T, xml string) Voucher {
	t.Helper()
	root, err := tally.Parse(xml)
	if err != nil {
		t.Fatal(err)
	}
	vs := root.FindAll("VOUCHER")
	if len(vs) != 1 {
		t.Fatalf("want 1 voucher, got %d", len(vs))
	}
	v, ok := parseVoucher(vs[0])
	if !ok {
		t.Fatalf("voucher did not balance: %+v", v.Entries)
	}
	return v
}

func TestInvoiceModeUsesAccountingAllocations(t *testing.T) {
	v := parseOne(t, `<TALLYMESSAGE><VOUCHER VCHTYPE="Sales"><DATE>20250405</DATE><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
	  <LEDGERENTRIES.LIST><LEDGERNAME>Acme</LEDGERNAME><AMOUNT>-11800.00</AMOUNT>
	    <BILLALLOCATIONS.LIST><NAME>INV-1</NAME><BILLTYPE>New Ref</BILLTYPE><AMOUNT>-11800.00</AMOUNT></BILLALLOCATIONS.LIST></LEDGERENTRIES.LIST>
	  <LEDGERENTRIES.LIST><LEDGERNAME>IGST Output</LEDGERNAME><AMOUNT>1800.00</AMOUNT></LEDGERENTRIES.LIST>
	  <ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>Widget</STOCKITEMNAME><AMOUNT>10000.00</AMOUNT>
	    <ACCOUNTINGALLOCATIONS.LIST><LEDGERNAME>Sales @ 18%</LEDGERNAME><AMOUNT>10000.00</AMOUNT></ACCOUNTINGALLOCATIONS.LIST>
	  </ALLINVENTORYENTRIES.LIST></VOUCHER></TALLYMESSAGE>`)
	if len(v.Entries) != 3 || v.Entries[0].Amount != 11800 || v.Entries[2].Ledger != "Sales @ 18%" || v.Entries[2].Amount != -10000 {
		t.Fatalf("entries: %+v", v.Entries)
	}
	if len(v.Entries[0].Bills) != 1 || v.Entries[0].Bills[0].Name != "INV-1" {
		t.Fatalf("bills: %+v", v.Entries[0].Bills)
	}
	if v.Date != "2025-04-05" {
		t.Fatalf("date %s", v.Date)
	}
}

func TestDuplicateListsAreNotDoubleCounted(t *testing.T) {
	v := parseOne(t, `<VOUCHER><DATE>20250405</DATE>
	  <ALLLEDGERENTRIES.LIST><LEDGERNAME>Rent</LEDGERNAME><AMOUNT>-500.00</AMOUNT></ALLLEDGERENTRIES.LIST>
	  <ALLLEDGERENTRIES.LIST><LEDGERNAME>Bank</LEDGERNAME><AMOUNT>500.00</AMOUNT></ALLLEDGERENTRIES.LIST>
	  <LEDGERENTRIES.LIST><LEDGERNAME>Rent</LEDGERNAME><AMOUNT>-500.00</AMOUNT></LEDGERENTRIES.LIST>
	  <LEDGERENTRIES.LIST><LEDGERNAME>Bank</LEDGERNAME><AMOUNT>500.00</AMOUNT></LEDGERENTRIES.LIST></VOUCHER>`)
	if len(v.Entries) != 2 {
		t.Fatalf("entries: %+v", v.Entries)
	}
}
