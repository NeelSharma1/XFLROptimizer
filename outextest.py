import os
aouts = os.getcwd()

def filenameGivenName(name):
    if name.__contains__("BETA"):
        return name[name.rindex("_") + 1:name.index("BETA")], "beta"
    else:
        return name[name.rindex("_") + 1:name.index("AOA")], "alpha"

outAlpha = open(aouts + "/fullOutAlpha.csv", "w")
outBeta = open(aouts + "/fullOutBeta.csv", "w")
out = outAlpha
for file in os.listdir(aouts):
    if file == "fullOutAlpha.csv" or file == "fullOutBeta.csv":
        continue
    f = open(f"{aouts}/{file}", "rb")
    lines = f.readlines()
    i = 1
    s = ""
    while not s.isnumeric() and lines[0] != lines[-i]:
        lastline = str(lines[-i])[2:-1]
        i += 1
        j = 0
        while j == 0 or s == "":
            s = lastline.split(" ")[j].strip()
            j += 1
    if not s.isnumeric():
        continue
    iter = True
    for i in lastline.split(" "):
        if i == "":
            continue
        if iter:
            i, ab = filenameGivenName(file)
            i = i.replace("p", ".")
            iter = False
            if ab == "alpha":
                out = outAlpha
            else:
                out = outBeta
        out.write(str(i.strip()) + ",")
    out.write("\n")
    f.close()