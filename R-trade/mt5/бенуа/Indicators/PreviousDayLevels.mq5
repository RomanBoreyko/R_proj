//+------------------------------------------------------------------+
//|                Previous Day High/Low/Open/Close Lines            |
//|                        © Roman Boreyko                          |
//+------------------------------------------------------------------+
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0

input color   ColorHigh   = clrLime;        // Цвет High
input color   ColorLow    = clrRed;         // Цвет Low
input color   ColorOpen   = clrDodgerBlue;  // Цвет Open
input color   ColorClose  = clrGold;        // Цвет Close
input int     LineWidth   = 1;              // Толщина линий
input ENUM_LINE_STYLE LineStyle = STYLE_DOT; // Стиль линий
input bool    ShowLabels  = true;           // Показывать подписи

datetime last_update = 0;

//--- функция создания или обновления линии
void DrawLine(string name, double price, color clr, string label)
{
   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_HLINE, 0, 0, price);
   }
   
      // обновляем свойства каждый раз при вызове
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE, LineStyle);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, LineWidth);
   ObjectSetDouble(0, name, OBJPROP_PRICE, price);

   if(ShowLabels)
      ObjectSetString(0, name, OBJPROP_TEXT, label + " " + DoubleToString(price, _Digits));
   else
      ObjectSetString(0, name, OBJPROP_TEXT, "");

}

//--- основная функция
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{
   datetime current_day = TimeCurrent();
   MqlDateTime t;
   TimeToStruct(current_day, t);
   t.hour = 0; t.min = 0; t.sec = 0;
   datetime today_start = StructToTime(t);
   datetime yesterday_start = today_start - 86400;
   datetime yesterday_end   = today_start - 1;

   if(last_update == today_start) return rates_total;
   last_update = today_start;

   int yShift = iBarShift(_Symbol, PERIOD_D1, yesterday_start, false);
   if(yShift < 0) return rates_total;

   double yHigh = iHigh(_Symbol, PERIOD_D1, 1);
   double yLow  = iLow(_Symbol, PERIOD_D1, 1);
   double yOpen = iOpen(_Symbol, PERIOD_D1, 1);
   double yClose= iClose(_Symbol, PERIOD_D1, 1);

   DrawLine("PDH", yHigh,  ColorHigh,  "Prev High");
   DrawLine("PDL", yLow,   ColorLow,   "Prev Low");
   DrawLine("PDO", yOpen,  ColorOpen,  "Prev Open");
   DrawLine("PDC", yClose, ColorClose, "Prev Close");

   return rates_total;
}
