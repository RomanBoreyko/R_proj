//+------------------------------------------------------------------------------------------------------------------+
//|                                                                              fractal_dimension.mq5               |
//|                                                                              Copyright © 2011, iliko             |
//|                                                                              arcsin5@netscape.net                |
//|                                                                                                                  |
//|  The Fractal Dimension Index determines the amount of market volatility. The easiest way to use this indicator is|
//|  to understand that a value of 1.5 suggests the market is acting in a completely random fashion.                 |
//|  As the market deviates from 1.5, the opportunity for earning profits is increased in proportion                 |
//|  to the amount of deviation.                                                                                     |
//|  But be carreful, the indicator does not show the direction of trends !!                                         |
//|                                                                                                                  |
//|  The indicator is red when the market is in a trend. And it is blue when there is a high volatility.             |
//|  When the FDI changes its color from red to blue, it means that a trend is finishing, the market becomes         |
//|  erratic and a high volatility is present. Usually, these "blue times" do not go for a long time.They come before|
//|  a new trend.                                                                                                    |
//|                                                                                                                  |
//|  For more informations, see                                                                                      |
//|  http://www.forex-tsd.com/suggestions-trading-systems/6119-tasc-03-07-fractal-dimension-index.html               |
//|                                                                                                                  |
//|                                                                                                                  |   
//|  HOW TO USE INPUT PARAMETERS :                                                                                   |   
//|  -----------------------------                                                                                   |   
//|                                                                                                                  |   
//|      1) e_period [ integer >= 1 ]                                              =>  30                            |   
//|                                                                                                                  |   
//|         The indicator will compute the historical market volatility over this period.                            |   
//|         Choose its value according to the average of trend lengths.                                              |   
//|                                                                                                                  |   
//|      2) e_type_data [ int = {PRICE_CLOSE_ = 1,     //Close                                                       |
//|                              PRICE_OPEN_,          //Open                                                        |
//|                              PRICE_HIGH_,          //High                                                        |
//|                              PRICE_LOW_,           //Low                                                         |
//|                              PRICE_MEDIAN_,        //Median Price (HL/2)                                         |
//|                              PRICE_TYPICAL_,       //Typical Price (HLC/3)                                       |
//|                              PRICE_WEIGHTED_,      //Weighted Close (HLCC/4)                                     |
//|                              PRICE_SIMPL_,         //Simpl Price (OC/2)                                          |
//|                              PRICE_QUARTER_,       //Quarted Price (HLOC/4)                                      |
//|                              PRICE_TRENDFOLLOW0_,  //TrendFollow_1 Price                                         |
//|                              PRICE_TRENDFOLLOW1_,  //TrendFollow_2 Price                                         |
//|                              PRICE_DEMARK_         //Demark Price}     => PRICE_CLOSE                            |   
//|                                                                                                                  |   
//|         Defines on which price type the Fractal Dimension is computed.                                           |   
//|                                                                                                                  |
//|      3) e_random_line [ 0.0 < double < 2.0 ]                                   => 1.5                            |
//|                                                                                                                  |
//|         Defines your separation betwen a trend market (red) and an erratic/high volatily one.                    |   
//|                                                                                                                  |   
//| v1.0 - February 2007                                                                                             |   
//+------------------------------------------------------------------------------------------------------------------+
#property copyright "Copyright © 2011, iliko"
#property link "arcsin5@netscape.net"
//--- Indicator version
#property version   "1.00"
//--- drawing the indicator in a separate window
#property indicator_separate_window 
//--- number of indicator buffers
#property indicator_buffers 2 
//--- one plot is used
#property indicator_plots   1
//+-----------------------------------+
//|  Parameters of indicator drawing  |
//+-----------------------------------+
//--- drawing the indicator as a multicolored line
#property indicator_type1   DRAW_COLOR_LINE
//--- the following colors are used in a three-color line
#property indicator_color1  clrRed,clrBlue
//--- the indicator line is a continuous curve
#property indicator_style1  STYLE_SOLID
//--- indicator line width is 2
#property indicator_width1  2
//--- displaying the indicator label
#property indicator_label1  "fractal_dimension"
//+-----------------------------------+
//|  Declaration of enumerations      |
//+-----------------------------------+
enum Applied_price_      // type of constant
  {
   PRICE_CLOSE_ = 1,     // Close
   PRICE_OPEN_,          // Open
   PRICE_HIGH_,          // High
   PRICE_LOW_,           // Low
   PRICE_MEDIAN_,        // Median Price (HL/2)
   PRICE_TYPICAL_,       // Typical Price (HLC/3)
   PRICE_WEIGHTED_,      // Weighted Close (HLCC/4)
   PRICE_SIMPL_,         // Simple Price (OC/2)
   PRICE_QUARTER_,       // Quarted Price (HLOC/4) 
   PRICE_TRENDFOLLOW0_,  // TrendFollow_1 Price 
   PRICE_TRENDFOLLOW1_,  // TrendFollow_2 Price 
   PRICE_DEMARK_         // Demark Price
  };
//+-----------------------------------+
//|  Indicator input parameters       |
//+-----------------------------------+
input uint                e_period=30;          // Period of averaging
input Applied_price_   e_type_data=PRICE_CLOSE; // Price constant
input double         e_random_line=1.5;         // Level of triggering
input int                    Shift=0;           // Horizontal shift of the indicator in bars
//+-----------------------------------+
//--- declaration of dynamic arrays that
//--- will be used as indicator buffers
double IndBuffer[],ColorIndBuffer[];
//--- declaration of the integer variables for the start of data calculation
int min_rates_total;
//--- declaration of global variables
int Count[];
double Price[];
double Log2,Log2e,Pow2e;
//+------------------------------------------------------------------+
//|  Recalculation of position of the newest element in the array    |
//+------------------------------------------------------------------+   
void Recount_ArrayZeroPos(int &CoArr[],// Return the current value of the price series by reference
                          int Size)
  {
//---
   int numb,Max1,Max2;
   static int count=1;

   Max2=Size;
   Max1=Max2-1;

   count--;
   if(count<0) count=Max1;

   for(int iii=0; iii<Max2; iii++)
     {
      numb=iii+count;
      if(numb>Max1) numb-=Max2;
      CoArr[iii]=numb;
     }
//---
  }
//+------------------------------------------------------------------+   
//| fractal_dimension indicator initialization function              | 
//+------------------------------------------------------------------+ 
void OnInit()
  {
//--- initialization of variables of the start of data calculation
   min_rates_total=int(e_period);
   Log2e=MathLog(2*e_period);
   Pow2e=1.0/MathPow(e_period,2.0);
   Log2=MathLog(2.0);
//--- memory distribution for variables' arrays  
   ArrayResize(Count,e_period);
   ArrayResize(Price,e_period);
//---
   ArrayInitialize(Count,0);
   ArrayInitialize(Price,0.0);
//--- indexing elements in the array as timeseries
   ArraySetAsSeries(Price,true);
//--- Set dynamic array as an indicator buffer
   SetIndexBuffer(0,IndBuffer,INDICATOR_DATA);
//--- Indexing elements in the buffer as in timeseries
   ArraySetAsSeries(IndBuffer,true);
//--- set dynamic array as a color index buffer   
   SetIndexBuffer(1,ColorIndBuffer,INDICATOR_COLOR_INDEX);
//--- Indexing elements in the buffer as in timeseries
   ArraySetAsSeries(ColorIndBuffer,true);
//--- shifting the indicator 1 horizontally
   PlotIndexSetInteger(0,PLOT_SHIFT,Shift);
//--- shifting the start of drawing of the indicator
   PlotIndexSetInteger(0,PLOT_DRAW_BEGIN,min_rates_total);
//--- setting the indicator values that won't be visible on a chart
   PlotIndexSetDouble(0,PLOT_EMPTY_VALUE,0.0);
//--- initializations of a variable for the indicator short name
   string shortname;
   StringConcatenate(shortname,"fractal_dimension(",
                     e_period,", ",EnumToString(e_type_data),", ",
                     DoubleToString(e_random_line,4),", ",Shift,")");
//--- Creation of the name to be displayed in a separate sub-window and in a pop up help
   IndicatorSetString(INDICATOR_SHORTNAME,shortname);
//--- Determining the accuracy of displaying the indicator values
   IndicatorSetInteger(INDICATOR_DIGITS,_Digits);
//--- the number of the indicator 1 horizontal levels  
   IndicatorSetInteger(INDICATOR_LEVELS,1);
//--- Values of the indicator horizontal levels   
   IndicatorSetDouble(INDICATOR_LEVELVALUE,0,e_random_line);
//--- the Purple color is used for the horizontal level line  
   IndicatorSetInteger(INDICATOR_LEVELCOLOR,0,clrPurple);
//--- Short dot-dash is used for the horizontal level line  
   IndicatorSetInteger(INDICATOR_LEVELSTYLE,0,STYLE_DASHDOTDOT);
//--- initialization end
  }
//+------------------------------------------------------------------+ 
//| fractal_dimension iteration function                             | 
//+------------------------------------------------------------------+ 
int OnCalculate(const int rates_total,    // number of bars in history at the current tick
                const int prev_calculated,// amount of history in bars at the previous tick
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {
//--- checking if the number of bars is enough for the calculation
   if(rates_total<min_rates_total) return(0);
//--- indexing elements in arrays as in timeseries
   ArraySetAsSeries(open,true);
   ArraySetAsSeries(high,true);
   ArraySetAsSeries(low,true);
   ArraySetAsSeries(close,true);
//--- declaration of variables with a floating point  
   double HH,LL,diff,priorDiff,length,fdi,Range;
//--- declaration of integer variables and getting already calculated bars
   int limit,bar,iii,clr;
//--- calculation of the 'first' starting index for the bars recalculation loop
   if(prev_calculated>rates_total || prev_calculated<=0) // checking for the first start of calculation of an indicator
      limit=rates_total-1;   // starting index for calculating all bars
   else limit=rates_total-prev_calculated; // Starting index for the calculation of new bars
//--- main indicator calculation loop
   for(bar=limit; bar>=0 && !IsStopped(); bar--)
     {
      //--- call of the PriceSeries function to get the input price
      Price[Count[0]]=PriceSeries(e_type_data,bar,open,low,high,close);
      //---
      HH=high[ArrayMaximum(high,bar,e_period)];
      LL=low[ArrayMinimum(low,bar,e_period)];
      Range=HH-LL;
      //---
      length=0.0;
      priorDiff=0.0;
      //---
      if(Range) for(iii=0; iii<int(e_period); iii++)
        {
         diff=(Price[Count[iii]]-LL)/Range;
         length+=MathSqrt(MathPow(diff-priorDiff,2.0)+Pow2e);
         priorDiff=diff;
        }
      //---
      if(length>0.0) fdi=1.0+(MathLog(length)+Log2)/Log2e;
      else
        {
         //--- The FDI algorithm suggests in this case a zero value.
         //--- I prefer to use the previous FDI value.
         fdi=0.0;
        }
      //---
      IndBuffer[bar]=fdi;
      if(bar) Recount_ArrayZeroPos(Count,e_period);
     }
//--- correction of the first variable value
   if(prev_calculated>rates_total || prev_calculated<=0) limit-=min_rates_total;
//--- main loop of the signal line coloring
   for(bar=limit; bar>=0 && !IsStopped(); bar--)
     {
      if(IndBuffer[bar]>=e_random_line) clr=1;
      else clr=0;
      ColorIndBuffer[bar]=clr;
     }
//---     
   return(rates_total);
  }
//+------------------------------------------------------------------+   
//| Getting values of a price time series                            |
//+------------------------------------------------------------------+ 
double PriceSeries(uint applied_price,  // Price constant
                   uint bar,            // Index of shift relative to the current bar for a specified number of periods back or forward).
                   const double &Open[],
                   const double &Low[],
                   const double &High[],
                   const double &Close[])
  {
   switch(applied_price)
     {
      //--- price constants from the ENUM_APPLIED_PRICE enumeration
      case  PRICE_CLOSE: return(Close[bar]);
      case  PRICE_OPEN: return(Open [bar]);
      case  PRICE_HIGH: return(High [bar]);
      case  PRICE_LOW: return(Low[bar]);
      case  PRICE_MEDIAN: return((High[bar]+Low[bar])/2.0);
      case  PRICE_TYPICAL: return((Close[bar]+High[bar]+Low[bar])/3.0);
      case  PRICE_WEIGHTED: return((2*Close[bar]+High[bar]+Low[bar])/4.0);
      //---                            
      case  8: return((Open[bar] + Close[bar])/2.0);
      case  9: return((Open[bar] + Close[bar] + High[bar] + Low[bar])/4.0);
      //---                                
      case 10:
        {
         if(Close[bar]>Open[bar])return(High[bar]);
         else
           {
            if(Close[bar]<Open[bar])
               return(Low[bar]);
            else return(Close[bar]);
           }
        }
      //---         
      case 11:
        {
         if(Close[bar]>Open[bar])return((High[bar]+Close[bar])/2.0);
         else
           {
            if(Close[bar]<Open[bar])
               return((Low[bar]+Close[bar])/2.0);
            else return(Close[bar]);
           }
         break;
        }
      //---         
      case 12:
        {
         double res=High[bar]+Low[bar]+Close[bar];
         if(Close[bar]<Open[bar]) res=(res+Low[bar])/2;
         if(Close[bar]>Open[bar]) res=(res+High[bar])/2;
         if(Close[bar]==Open[bar]) res=(res+Close[bar])/2;
         return(((res-Low[bar])+(res-High[bar]))/2);
        }
      //---
      default: return(Close[bar]);
     }
//---
  }
//+------------------------------------------------------------------+
