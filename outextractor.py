import os
import shutil

cwd = os.getcwd()
dirlist = os.listdir(cwd)
aouts = cwd + "/AOuts/"
os.mkdir(aouts)

for dir in dirlist:
    if not os.path.isdir(dir) or not os.listdir(dir).__contains__("starccm.out"):
        continue
    dirpath = cwd + "/" + dir + "/"

    shutil.copy(dirpath + "starccm.out", aouts + dir + ".out")
    shutil.copy(dirpath + "Residuals_residuals_0000.000.png", aouts + dir + "_residuals.png")
    shutil.copy(dirpath + "Lift_Plot_lift_0000.000.png", aouts + dir + "_lift.png")
    shutil.copy(dirpath + "Moments_Plot_moments_0000.000.png", aouts + dir + "_moment.png")

def filenameGivenName(name):
    if name.__contains__("BETA"):
        return name[name.rindex("_") + 1:name.index("BETA")], "beta"
    elif name.__contains__("AOA"):
        return name[name.rindex("_") + 1:name.index("AOA")], "alpha"
    return None, None

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
            if i == None:
                break
            i = i.replace("p", ".")
            iter = False
            if ab == "alpha":
                out = outAlpha
            else:
                out = outBeta
        out.write(str(i.strip()) + ",")
    out.write("\n")
    f.close()