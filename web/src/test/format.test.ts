import { describe, expect, it } from "vitest";
import { decimal, money, moneyInputError, pnl, price, sign } from "../lib/format";

describe("money formatting is exact (string/BigInt, no floats)", () => {
  it("rounds half to even and groups thousands", () => {
    expect(decimal("1234.5650")).toBe("1,234.56");
    expect(decimal("1234.5750")).toBe("1,234.58");
    expect(decimal("0.0050")).toBe("0.00");
    expect(decimal("0.0150")).toBe("0.02");
    expect(decimal("-302.6000")).toBe("−302.60");
    expect(decimal("-0.0040")).toBe("0.00");
    expect(decimal("1000000.0000", 0)).toBe("1,000,000");
    expect(decimal("1.2345", 4)).toBe("1.2345");          // nothing dropped, nothing rounded
  });
  it("keeps precision that floats would lose", () => {
    expect(decimal("9007199254740993.1234", 4)).toBe("9,007,199,254,740,993.1234");
    expect(money("0.1000")).toBe("$0.10");
  });
  it("formats dollars, P&L and prices", () => {
    expect(money("-302.6")).toBe("−$302.60");
    expect(money(null)).toBeNull();
    expect(pnl("12")).toBe("+$12.00");
    expect(pnl("-3.4")).toBe("−$3.40");
    expect(pnl("0")).toBe("$0.00");
    expect(price("1.2500")).toBe("$1.25");
    expect(price("1.2345")).toBe("$1.2345");
    expect(price("432.5000")).toBe("$432.50");
    expect(sign("-0.0001")).toBe(-1);
  });
  it("validates money inputs like the API (no rounding of inputs)", () => {
    expect(moneyInputError("1.2345")).toBeNull();
    expect(moneyInputError("1.23456")).toMatch(/4 decimal places/);
    expect(moneyInputError("-1")).toMatch(/positive/);
    expect(moneyInputError("-1", true)).toBeNull();
    expect(moneyInputError("abc")).not.toBeNull();
  });
});
