# Governed COBOL batch pack

Compile and execute from the `src` directory with GnuCOBOL:

```sh
cobc -x -std=ibm -fformat=fixed -I . BATCHPROC.cob -o BATCHPROC
cp ../sample-input.dat ./sample-input.dat
./BATCHPROC
```

The program fails closed with return code 12 on input/output failures. It validates each
record, maintains COMP-3 monetary and record control totals, checks `FILE STATUS` after
every operation, and writes accepted, rejected, and final control-total records.
